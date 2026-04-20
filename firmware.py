"""
PCB Hotplate Controller Firmware
Target: Raspberry Pi Pico (RP2040) running CircuitPython
Hardware: ST7735S LCD, CH224A PD controller, NTC thermistor, TLV9002 current sense

Required CircuitPython libraries (copy to /lib on CIRCUITPY):
  - adafruit_st7735r
  - adafruit_display_text
  - adafruit_bitmap_font  (optional, for nicer fonts)
  - adafruit_bus_device
  - adafruit_display_shapes
"""

import time
import math
import board
import busio
import digitalio
import analogio
import pwmio
import displayio
import terminalio

from adafruit_st7735r import ST7735R
from adafruit_display_text import label
from adafruit_display_shapes.rect import Rect

# ─────────────────────────────────────────────
#  Pin Definitions
# ─────────────────────────────────────────────
# I2C (CH224A PD controller)
I2C_SCL_PIN  = board.GP5
I2C_SDA_PIN  = board.GP4

# PWM (MOSFET gate drive)
GATE_IN_PIN  = board.GP2

# ADC
ADC0_PIN     = board.GP26   # Current sense
ADC1_PIN     = board.GP27   # NTC temperature
ADC2_PIN     = board.GP28   # VDD voltage sense

# Buttons (active low, internal pull-up)
BTN1_PIN     = board.GP18   # Up / scroll up
BTN2_PIN     = board.GP19   # Select / Start
BTN3_PIN     = board.GP20   # Down / scroll down
BTN4_PIN     = board.GP21   # Stop / Back

# LEDs
LED1_PIN     = board.GP0
LED2_PIN     = board.GP1

# SPI / ST7735S LCD
LCD_SCK_PIN  = board.GP10
LCD_SDA_PIN  = board.GP11
LCD_DC_PIN   = board.GP12
LCD_CS_PIN   = board.GP13
LCD_RST_PIN  = board.GP9

# ─────────────────────────────────────────────
#  Hardware Constants
# ─────────────────────────────────────────────
# CH224A
CH224A_ADDR        = 0x68
CH224A_VOLT_REG    = 0x02

# ADC reference
ADC_REF_V          = 3.3
ADC_MAX            = 65535

# NTC (NDBG104F3380B1F)
NTC_NOMINAL_R      = 10000.0   # 10 kΩ at 25 °C
NTC_BETA           = 3380.0
NTC_SERIES_R       = 10000.0   # fixed resistor (low side)
NTC_NOMINAL_T      = 298.15    # 25 °C in Kelvin

# Current sense
OPAMP_GAIN         = 1 + (49900 / 1000)   # 50.9
SHUNT_R            = 0.01                  # 10 mΩ

# Voltage divider
VDIV_MULTIPLIER    = (82000 + 10000) / 10000   # 9.2×

# PWM
PWM_FREQ           = 100_000   # 100 kHz
PWM_MAX            = 65535

# ─────────────────────────────────────────────
#  PID Tuning (tune for your thermal mass)
# ─────────────────────────────────────────────
PID_KP             = 800.0
PID_KI             = 2.0
PID_KD             = 120.0
PID_I_MAX          = 30000.0   # anti-windup clamp

# ─────────────────────────────────────────────
#  Reflow Profiles
# ─────────────────────────────────────────────
# Each phase: (name, target_temp_C, duration_s, max_ramp_C_per_s)
# Ramp enforcement is handled in the PID setpoint ramp logic.
PROFILES = {
    "Sn63/Pb37": [
        ("Preheat",  100, 60,  2.0),
        ("Soak",     150, 90,  1.0),   # slow ramp into soak
        ("Ramp",     183, 30,  4.0),   # max 4 °C/s to liquidus
        ("Reflow",   210, 20,  4.0),   # peak
        ("Cooldown",  25, 60, -4.0),   # forced cool (PWM off, just tracking)
    ],
    "SAC305 (Pb-free)": [
        ("Preheat",  150, 60,  2.0),
        ("Soak",     200, 60,  1.0),
        ("Ramp",     250, 30,  4.0),
        ("Reflow",   260, 15,  4.0),
        ("Cooldown",  25, 60, -4.0),
    ],
}

PROFILE_NAMES      = list(PROFILES.keys())

# ─────────────────────────────────────────────
#  LED State Machine
# ─────────────────────────────────────────────
LED_IDLE           = 0
LED_HEATING        = 1
LED_HOT            = 2   # > 60 °C
LED_SUPER_HOT      = 3   # > 100 °C

# ─────────────────────────────────────────────
#  UI States
# ─────────────────────────────────────────────
UI_MENU            = "menu"
UI_RUNNING         = "running"
UI_MANUAL          = "manual"
UI_DONE            = "done"

# ─────────────────────────────────────────────
#  Initialise Hardware
# ─────────────────────────────────────────────

def init_hardware():
    """Initialise all peripherals and return a dict of handles."""
    hw = {}

    # ── I2C ──────────────────────────────────
    hw["i2c"] = busio.I2C(I2C_SCL_PIN, I2C_SDA_PIN, frequency=400_000)

    # ── PWM (MOSFET) ─────────────────────────
    pwm = pwmio.PWMOut(GATE_IN_PIN, frequency=PWM_FREQ, duty_cycle=0)
    hw["pwm"] = pwm

    # ── ADC ──────────────────────────────────
    hw["adc_current"] = analogio.AnalogIn(ADC0_PIN)
    hw["adc_ntc"]     = analogio.AnalogIn(ADC1_PIN)
    hw["adc_voltage"] = analogio.AnalogIn(ADC2_PIN)

    # ── Buttons ──────────────────────────────
    def make_btn(pin):
        b = digitalio.DigitalInOut(pin)
        b.direction = digitalio.Direction.INPUT
        b.pull = digitalio.Pull.UP
        return b

    hw["btn1"] = make_btn(BTN1_PIN)
    hw["btn2"] = make_btn(BTN2_PIN)
    hw["btn3"] = make_btn(BTN3_PIN)
    hw["btn4"] = make_btn(BTN4_PIN)

    # ── LEDs ─────────────────────────────────
    def make_led(pin):
        l = digitalio.DigitalInOut(pin)
        l.direction = digitalio.Direction.OUTPUT
        l.value = False
        return l

    hw["led1"] = make_led(LED1_PIN)
    hw["led2"] = make_led(LED2_PIN)

    # ── SPI / LCD ────────────────────────────
    displayio.release_displays()
    spi = busio.SPI(clock=LCD_SCK_PIN, MOSI=LCD_SDA_PIN)
    dc  = digitalio.DigitalInOut(LCD_DC_PIN)
    cs  = digitalio.DigitalInOut(LCD_CS_PIN)
    rst = digitalio.DigitalInOut(LCD_RST_PIN)

    display_bus = displayio.FourWire(spi, command=dc, chip_select=cs, reset=rst)
    # ST7735S  — 128×160, adjust rotation/dimensions to your panel orientation
    display = ST7735R(display_bus, width=128, height=160, rotation=0,
                      bgr=True, auto_refresh=False)
    hw["display"] = display

    return hw


# ─────────────────────────────────────────────
#  CH224A Power Delivery
# ─────────────────────────────────────────────

# Voltage ladder: (label, register_byte, minimum_expected_vdd)
# CH224A register 0x02 encoding per datasheet:
#   0x06 → 28 V,  0x05 → 20 V,  0x04 → 15 V,
#   0x03 → 12 V,  0x02 → 9 V,   0x01 → 5 V
CH224A_VOLTAGE_LADDER = [
    ("28V", 0x06, 26.0),
    ("20V", 0x05, 18.0),
    ("15V", 0x04, 13.5),
    ("12V", 0x03, 10.5),
    ("9V",  0x02,  8.0),
    ("5V",  0x01,  4.5),
]
# Settle time after each request before reading VDD back
CH224A_SETTLE_S = 0.15


def _ch224a_write(i2c, reg_byte):
    """Low-level: write one byte to CH224A voltage register. Returns True on success."""
    while not i2c.try_lock():
        pass
    try:
        i2c.writeto(CH224A_ADDR, bytes([CH224A_VOLT_REG, reg_byte]))
        return True
    except OSError as e:
        print(f"[CH224A] I2C error: {e}")
        return False
    finally:
        i2c.unlock()


def ch224a_negotiate(i2c, adc_voltage_hw):
    """
    Walk the voltage ladder from highest to lowest.
    After each request, read VDD back to confirm the source accepted it.
    Returns the negotiated voltage string (e.g. "20V") or "unknown" if
    nothing confirmed above 5 V — we still leave whatever the source gave us.
    """
    for label, cmd, min_vdd in CH224A_VOLTAGE_LADDER:
        print(f"[CH224A] Requesting {label}...")
        if not _ch224a_write(i2c, cmd):
            continue                         # I2C fault, try next
        time.sleep(CH224A_SETTLE_S)
        vdd = read_vdd(adc_voltage_hw)
        print(f"[CH224A]   VDD reads {vdd:.2f} V (need ≥{min_vdd} V)")
        if vdd >= min_vdd:
            print(f"[CH224A] Locked at {label} ({vdd:.2f} V)")
            return label
        # Source didn't deliver — try next step down
    print("[CH224A] Could not confirm any voltage; proceeding with whatever source provides")
    return "unknown"


# ─────────────────────────────────────────────
#  Sensor Readings
# ─────────────────────────────────────────────

def read_voltage(adc_raw):
    """Convert raw ADC value to voltage at ADC pin."""
    return (adc_raw / ADC_MAX) * ADC_REF_V


def read_temperature_c(hw):
    """
    NTC on high side, 10 kΩ fixed on low side (GND).
    V_adc = Vcc × (R_fixed / (R_ntc + R_fixed))
    → R_ntc = R_fixed × (Vcc/V_adc − 1)
    """
    v_adc = read_voltage(hw["adc_ntc"].value)
    if v_adc < 0.001:
        return 999.0   # open circuit / disconnected
    r_ntc = NTC_SERIES_R * ((ADC_REF_V / v_adc) - 1.0)
    # Steinhart–Hart (β approximation)
    temp_k = NTC_BETA / (math.log(r_ntc / NTC_NOMINAL_R) + (NTC_BETA / NTC_NOMINAL_T))
    return temp_k - 273.15


def read_current_a(hw):
    """
    V_adc = I × R_shunt × Gain
    → I = V_adc / (Gain × R_shunt)
    """
    v_adc = read_voltage(hw["adc_current"].value)
    return v_adc / (OPAMP_GAIN * SHUNT_R)


def read_vdd(hw):
    """Recover actual VDD via the 82k/10k divider (9.2× multiplier)."""
    v_adc = read_voltage(hw["adc_voltage"].value)
    return v_adc * VDIV_MULTIPLIER


# ─────────────────────────────────────────────
#  PID Controller
# ─────────────────────────────────────────────

class PID:
    def __init__(self, kp, ki, kd, i_max=PID_I_MAX):
        self.kp = kp
        self.ki = ki
        self.kd = kd
        self.i_max = i_max
        self.integral = 0.0
        self.prev_error = 0.0
        self.prev_time = None

    def reset(self):
        self.integral = 0.0
        self.prev_error = 0.0
        self.prev_time = None

    def compute(self, setpoint, measured):
        now = time.monotonic()
        if self.prev_time is None:
            dt = 0.1
        else:
            dt = now - self.prev_time
            if dt <= 0:
                dt = 0.001
        self.prev_time = now

        error = setpoint - measured

        self.integral += error * dt
        self.integral = max(-self.i_max, min(self.i_max, self.integral))

        derivative = (error - self.prev_error) / dt
        self.prev_error = error

        output = (self.kp * error) + (self.ki * self.integral) + (self.kd * derivative)
        return max(0, min(PWM_MAX, int(output)))


# ─────────────────────────────────────────────
#  Reflow State Machine
# ─────────────────────────────────────────────

class ReflowRunner:
    """Executes a multi-phase reflow profile with ramp rate enforcement."""

    def __init__(self, profile_phases, pid):
        self.phases = profile_phases
        self.pid = pid
        self.phase_idx = 0
        self.phase_start_time = None
        self.phase_start_temp = None
        self.done = False
        self.current_setpoint = 0.0

    def start(self, current_temp):
        self.phase_idx = 0
        self.phase_start_time = time.monotonic()
        self.phase_start_temp = current_temp
        self.done = False
        self.pid.reset()
        self.current_setpoint = current_temp
        print("[Reflow] Started")

    def update(self, current_temp):
        """
        Call every control loop tick.
        Returns (duty_cycle, phase_name, setpoint, done).
        """
        if self.done:
            return 0, "Done", self.current_setpoint, True

        phase_name, target_temp, duration_s, max_ramp = self.phases[self.phase_idx]
        now = time.monotonic()
        elapsed = now - self.phase_start_time

        # Ramp the setpoint toward target at max_ramp °C/s
        ramp_direction = 1 if target_temp > self.phase_start_temp else -1
        ramped_target = self.phase_start_temp + (max_ramp * elapsed * ramp_direction)
        if ramp_direction > 0:
            self.current_setpoint = min(ramped_target, target_temp)
        else:
            self.current_setpoint = max(ramped_target, target_temp)

        # Cooldown phase: turn off heater, just track
        if "ooldown" in phase_name:
            duty = 0
        else:
            duty = self.pid.compute(self.current_setpoint, current_temp)

        # Phase transition: time expired AND temperature within ±5 °C of target
        temp_ok = abs(current_temp - target_temp) < 5.0
        if elapsed >= duration_s and temp_ok:
            self.phase_idx += 1
            if self.phase_idx >= len(self.phases):
                self.done = True
                return 0, "Done", self.current_setpoint, True
            # Start next phase
            self.phase_start_time = now
            self.phase_start_temp = current_temp
            phase_name = self.phases[self.phase_idx][0]
            print(f"[Reflow] → {phase_name}")

        return duty, phase_name, self.current_setpoint, False


# ─────────────────────────────────────────────
#  LED State Machine
# ─────────────────────────────────────────────

class LEDController:
    def __init__(self, led1, led2):
        self.led1 = led1
        self.led2 = led2
        self.state = LED_IDLE
        self._last_toggle = 0.0
        self._flash_on = False

    def set_state(self, temperature, is_heating):
        # HOT and SUPER_HOT warnings are temperature-only — they persist
        # regardless of whether the heater is currently active, because
        # the plate is still dangerous while it cools down.
        if temperature > 100:
            self.state = LED_SUPER_HOT
        elif temperature > 60:
            self.state = LED_HOT
        elif is_heating:
            self.state = LED_HEATING
        else:
            self.state = LED_IDLE

    def update(self):
        now = time.monotonic()
        half_period = 0.25   # 2 Hz → 0.5 s period → 0.25 s half

        if self.state == LED_IDLE:
            self.led1.value = False
            self.led2.value = False

        elif self.state == LED_HEATING:
            if now - self._last_toggle >= half_period:
                self._flash_on = not self._flash_on
                self._last_toggle = now
            self.led1.value = self._flash_on
            self.led2.value = False

        elif self.state == LED_HOT:
            if now - self._last_toggle >= half_period:
                self._flash_on = not self._flash_on
                self._last_toggle = now
            self.led1.value = self._flash_on
            self.led2.value = True

        elif self.state == LED_SUPER_HOT:
            if now - self._last_toggle >= half_period:
                self._flash_on = not self._flash_on
                self._last_toggle = now
            self.led1.value = self._flash_on
            self.led2.value = not self._flash_on   # alternating


# ─────────────────────────────────────────────
#  Display / UI
# ─────────────────────────────────────────────

class HotplateUI:
    """
    Simple display manager for the ST7735S (128×160).
    Builds a displayio group with text labels and redraws on update().
    """
    FONT = terminalio.FONT
    W, H = 128, 160

    # Colour palette (RGB565 packed as int)
    C_BG      = 0x0A0A0A
    C_ACCENT  = 0xFF6600
    C_WHITE   = 0xFFFFFF
    C_GREEN   = 0x00FF80
    C_RED     = 0xFF2020
    C_YELLOW  = 0xFFDD00
    C_GREY    = 0x555555

    # Temperature colour ramp — (threshold_°C, temp_label_colour, title_bar_colour)
    # Applied in order; first threshold that temp_c is BELOW wins.
    TEMP_RAMP = [
        ( 40,  0x00CFFF, 0x004466),   # cool  → icy blue
        ( 60,  0x00FF80, 0x005533),   # warm  → green
        (100,  0xFFDD00, 0x554400),   # hot   → yellow
        (150,  0xFF8800, 0x552200),   # very hot → orange
        (999,  0xFF2020, 0x550000),   # super hot → red
    ]

    @classmethod
    def _temp_colours(cls, temp_c):
        """Return (temp_text_colour, title_bar_colour) for the given temperature."""
        for threshold, text_col, bar_col in cls.TEMP_RAMP:
            if temp_c < threshold:
                return text_col, bar_col
        return cls.C_RED, 0x550000

    def __init__(self, display):
        self.display = display
        self.group = displayio.Group()

        # Background
        bg_bmp = displayio.Bitmap(self.W, self.H, 1)
        bg_pal = displayio.Palette(1)
        bg_pal[0] = self.C_BG
        self.group.append(displayio.TileGrid(bg_bmp, pixel_shader=bg_pal))

        # Title bar rect
        self.title_rect = Rect(0, 0, self.W, 16, fill=self.C_ACCENT)
        self.group.append(self.title_rect)

        def make_label(text, x, y, color=0xFFFFFF, scale=1):
            lbl = label.Label(self.FONT, text=text, color=color, scale=scale)
            lbl.x = x
            lbl.y = y
            return lbl

        self.lbl_title   = make_label("PCB Hotplate", 4, 8,  0x0A0A0A)
        self.lbl_temp    = make_label("---.-°C",       4, 30, self.C_WHITE, scale=2)
        self.lbl_set     = make_label("Set: ---°C",    4, 60, self.C_YELLOW)
        self.lbl_phase   = make_label("Phase: ---",    4, 76, self.C_GREEN)
        self.lbl_current = make_label("I: -.-- A",     4, 92, self.C_WHITE)
        self.lbl_voltage = make_label("V: --.-- V",    4, 108, self.C_WHITE)
        self.lbl_duty    = make_label("Duty: ----%",   4, 124, self.C_GREY)
        self.lbl_status  = make_label("IDLE",          4, 144, self.C_ACCENT)

        for lbl in (self.lbl_title, self.lbl_temp, self.lbl_set, self.lbl_phase,
                    self.lbl_current, self.lbl_voltage, self.lbl_duty, self.lbl_status):
            self.group.append(lbl)

        self.display.root_group = self.group
        self.display.refresh()

        # Menu state
        self.menu_sel = 0

    def update_run(self, temp, setpoint, phase, current, voltage, duty, status):
        # Reactive colours based on live temperature
        temp_col, bar_col = self._temp_colours(temp)
        self.lbl_temp.color       = temp_col
        self.title_rect.fill      = bar_col

        self.lbl_temp.text    = f"{temp:5.1f}C"
        self.lbl_set.text     = f"Set:{setpoint:5.1f}C"
        self.lbl_phase.text   = f"{phase[:16]}"
        self.lbl_current.text = f"I:{current:5.2f}A"
        self.lbl_voltage.text = f"V:{voltage:5.2f}V"
        duty_pct = duty / PWM_MAX * 100
        self.lbl_duty.text    = f"Duty:{duty_pct:5.1f}%"
        self.lbl_status.text  = status[:14]
        self.display.refresh()

    def show_menu(self, profile_names, selected_idx, manual_target):
        self.lbl_title.text   = "PCB Hotplate"
        self.lbl_temp.text    = "MENU"
        self.lbl_set.text     = ""
        self.lbl_duty.text    = ""
        self.lbl_current.text = ""
        self.lbl_voltage.text = ""
        lines = []
        for i, name in enumerate(profile_names):
            prefix = ">" if i == selected_idx else " "
            lines.append(f"{prefix}{name[:14]}")
        # Last option: manual mode
        prefix = ">" if selected_idx == len(profile_names) else " "
        lines.append(f"{prefix}Manual {manual_target:.0f}C")

        self.lbl_phase.text  = lines[0] if len(lines) > 0 else ""
        self.lbl_status.text = lines[1] if len(lines) > 1 else ""
        self.display.refresh()

    def show_done(self, peak_temp):
        self.lbl_title.text   = "PCB Hotplate"
        self.lbl_temp.text    = "DONE"
        self.lbl_set.text     = f"Peak:{peak_temp:.1f}C"
        self.lbl_phase.text   = "Reflow complete"
        self.lbl_current.text = ""
        self.lbl_voltage.text = ""
        self.lbl_duty.text    = ""
        self.lbl_status.text  = "BTN4 to return"
        self.display.refresh()


# ─────────────────────────────────────────────
#  Button Debounce Helper
# ─────────────────────────────────────────────

class DebouncedButton:
    DEBOUNCE_MS = 50

    def __init__(self, pin_obj):
        self._pin = pin_obj
        self._last_state = True    # pull-up → idle HIGH
        self._last_change = 0.0
        self.pressed = False       # True for exactly one tick on press

    def update(self):
        now = time.monotonic() * 1000
        raw = self._pin.value
        self.pressed = False
        if raw != self._last_state and (now - self._last_change) > self.DEBOUNCE_MS:
            self._last_state = raw
            self._last_change = now
            if not raw:             # falling edge = press (pull-up)
                self.pressed = True


# ─────────────────────────────────────────────
#  Main Application
# ─────────────────────────────────────────────

def main():
    print("[Boot] Initialising hardware...")
    hw = init_hardware()

    # ── Negotiate best available PD voltage ───
    print("[Boot] Negotiating USB-C PD voltage via CH224A...")
    negotiated_v = ch224a_negotiate(hw["i2c"], hw["adc_voltage"])
    print(f"[Boot] PD settled at {negotiated_v}")

    # ── Peripherals ───────────────────────────
    pid     = PID(PID_KP, PID_KI, PID_KD)
    leds    = LEDController(hw["led1"], hw["led2"])
    ui      = HotplateUI(hw["display"])

    btns = {
        "btn1": DebouncedButton(hw["btn1"]),
        "btn2": DebouncedButton(hw["btn2"]),
        "btn3": DebouncedButton(hw["btn3"]),
        "btn4": DebouncedButton(hw["btn4"]),
    }

    # ── Application State ─────────────────────
    ui_state       = UI_MENU
    profile_sel    = 0              # index into PROFILE_NAMES + 1 manual slot
    manual_target  = 100.0         # °C for manual mode
    reflow         = None
    is_heating     = False
    duty           = 0
    phase_name     = "Idle"
    setpoint       = 0.0
    peak_temp      = 0.0
    last_display   = 0.0

    DISPLAY_INTERVAL = 0.15        # seconds between display refreshes

    print("[Boot] Ready. Entering main loop.")

    while True:
        now = time.monotonic()

        # ── Sensor Readings ───────────────────
        temp_c   = read_temperature_c(hw)
        current  = read_current_a(hw)
        voltage  = read_vdd(hw)

        # ── Button Updates ────────────────────
        for b in btns.values():
            b.update()

        b1 = btns["btn1"].pressed   # Up / increment
        b2 = btns["btn2"].pressed   # Select / Start
        b3 = btns["btn3"].pressed   # Down / decrement
        b4 = btns["btn4"].pressed   # Stop / Back

        # ── Safety Cutoff ─────────────────────
        # Hard stop at 280 °C or if current > 6 A (fault)
        if temp_c > 280 or current > 6.0:
            hw["pwm"].duty_cycle = 0
            is_heating  = False
            duty        = 0
            ui_state    = UI_MENU
            phase_name  = "FAULT"
            print(f"[SAFETY] Cutoff! temp={temp_c:.1f} current={current:.2f}")

        # ─────────────────────────────────────
        #  UI State Machine
        # ─────────────────────────────────────

        if ui_state == UI_MENU:
            is_heating = False
            duty = 0
            hw["pwm"].duty_cycle = 0
            total_options = len(PROFILE_NAMES) + 1   # profiles + manual

            if b1:
                profile_sel = (profile_sel - 1) % total_options
            if b3:
                profile_sel = (profile_sel + 1) % total_options

            # In manual slot: BTN1/BTN3 fine-tune target temp
            in_manual = (profile_sel == len(PROFILE_NAMES))
            if in_manual:
                if b1:
                    manual_target = min(300, manual_target + 5)
                if b3:
                    manual_target = max(20, manual_target - 5)

            if b2:   # Start
                if in_manual:
                    ui_state   = UI_MANUAL
                    setpoint   = manual_target
                    is_heating = True
                    pid.reset()
                    phase_name = "Manual"
                    print(f"[Mode] Manual → {manual_target:.0f}°C")
                else:
                    profile_key = PROFILE_NAMES[profile_sel]
                    phases = PROFILES[profile_key]
                    reflow = ReflowRunner(phases, pid)
                    reflow.start(temp_c)
                    ui_state   = UI_RUNNING
                    is_heating = True
                    phase_name = phases[0][0]
                    peak_temp  = temp_c
                    print(f"[Mode] Reflow profile: {profile_key}")

            if now - last_display > DISPLAY_INTERVAL:
                ui.show_menu(PROFILE_NAMES, profile_sel, manual_target)
                last_display = now

        elif ui_state == UI_MANUAL:
            # Simple PID hold at manual_target
            if b1:
                manual_target = min(300, manual_target + 5)
                setpoint = manual_target
            if b3:
                manual_target = max(20, manual_target - 5)
                setpoint = manual_target

            duty = pid.compute(setpoint, temp_c)
            hw["pwm"].duty_cycle = duty

            if b4:   # Stop
                hw["pwm"].duty_cycle = 0
                ui_state   = UI_MENU
                is_heating = False
                phase_name = "Idle"
                duty       = 0

            if now - last_display > DISPLAY_INTERVAL:
                ui.update_run(temp_c, setpoint, "Manual",
                              current, voltage, duty, "MANUAL")
                last_display = now

        elif ui_state == UI_RUNNING:
            duty, phase_name, setpoint, done = reflow.update(temp_c)
            hw["pwm"].duty_cycle = duty
            peak_temp = max(peak_temp, temp_c)

            if b4:   # Abort
                hw["pwm"].duty_cycle = 0
                ui_state   = UI_MENU
                is_heating = False
                duty       = 0
                reflow     = None
                print("[Reflow] Aborted by user")

            elif done:
                hw["pwm"].duty_cycle = 0
                is_heating = False
                duty       = 0
                ui_state   = UI_DONE
                print(f"[Reflow] Complete. Peak temp: {peak_temp:.1f}°C")

            if now - last_display > DISPLAY_INTERVAL:
                status = "RUNNING" if not done else "DONE"
                ui.update_run(temp_c, setpoint, phase_name,
                              current, voltage, duty, status)
                last_display = now

        elif ui_state == UI_DONE:
            is_heating = False
            hw["pwm"].duty_cycle = 0
            duty = 0

            if b4 or b2:
                ui_state = UI_MENU

            if now - last_display > DISPLAY_INTERVAL:
                ui.show_done(peak_temp)
                last_display = now

        # ── LED Update ────────────────────────
        leds.set_state(temp_c, is_heating)
        leds.update()

        # Small sleep to yield CPU (adjust for desired loop rate)
        time.sleep(0.01)


# ─────────────────────────────────────────────
#  Entry Point
# ─────────────────────────────────────────────
main()