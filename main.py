import math
import struct
import time
from machine import ADC, PWM, I2C, Pin
import framebuf
import network
import socket
try:
    import ssl
except ImportError:
    import ussl as ssl

# Import credentials from config.py
import config

print("\n--- STARTING AD8232 (ECG) + MPU6050 + BUZZER + TELEGRAM ALERT ---")

# --- Alarm settings ---
ALARM_G = 3.0           # DROP alarm threshold (g)
ALARM_DURATION_MS = 10000  # DROP alarm: buzzer on for 10 s
MOVE_BEEP_MS = 300      # MOVING: short beep (0.3 s)
FREEFALL_G = 0.3         # Free-fall threshold
DROP_BEEP_MS = 300      # Short DROP beep

alarm_active = False
alarm_armed = True
alarm_start = 0
alarm_g = 0.0

move_beep_active = False
move_armed = True
move_beep_start = 0
beep_len = MOVE_BEEP_MS
drop_beep_armed = True
telegram_pending = False


# --- Wi-Fi Connection Function ---
def connect_wifi():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if not wlan.isconnected():
        print(f"[+] Connecting to Wi-Fi: {config.WIFI_SSID}...")
        wlan.connect(config.WIFI_SSID, config.WIFI_PASS)
        attempts = 0
        while not wlan.isconnected() and attempts < 20:
            time.sleep(0.5)
            attempts += 1
    if wlan.isconnected():
        print(f"[+] Wi-Fi Connected! IP: {wlan.ifconfig()[0]}")
    else:
        print("[!] Wi-Fi Connection Failed (Running offline mode)")


connect_wifi()

# --- Telegram Alert Function ---
TELEGRAM_TIMEOUT_S = 4


def url_encode(text):
    safe = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.~"
    out = ""
    for b in text.encode():
        c = chr(b)
        out += c if c in safe else "%{:02X}".format(b)
    return out


def tls_wrap(sock, host):
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.verify_mode = ssl.CERT_NONE
        return ctx.wrap_socket(sock, server_hostname=host)
    except AttributeError:
        return ssl.wrap_socket(sock, server_hostname=host)


def send_telegram_alert(g_force):
    if not network.WLAN(network.STA_IF).isconnected():
        print("[!] Telegram skipped: Wi-Fi not connected")
        return
    host = "api.telegram.org"
    message = f"\U0001F6A8 DROP DETECTED! Force: {g_force:.2f}g."
    path = f"/bot{config.TELEGRAM_TOKEN}/sendMessage?chat_id={config.TELEGRAM_CHAT_ID}&text={url_encode(message)}"
    request = f"GET {path} HTTP/1.0\r\nHost: {host}\r\nConnection: close\r\n\r\n"
    s = None
    try:
        addr = socket.getaddrinfo(host, 443)[0][-1]
        s = socket.socket()
        s.settimeout(TELEGRAM_TIMEOUT_S)
        s.connect(addr)
        s = tls_wrap(s, host)
        s.write(request.encode())
        reply = s.read(64)
        if reply and b" 200" in reply:
            print("[+] Telegram notification sent!")
        else:
            print(f"[!] Telegram rejected message: {reply}")
    except Exception as e:
        print(f"[!] Failed to send Telegram alert: {e}")
    finally:
        try:
            if s:
                s.close()
        except Exception:
            pass


# --- Fixed-Orientation OLED Driver ---
class FixedOLED(framebuf.FrameBuffer):
    def __init__(self, width, height, i2c, addr=0x3C):
        self.i2c = i2c
        self.addr = addr
        self.width = width
        self.height = height
        self.buffer = bytearray(self.height * self.width // 8)
        super().__init__(self.buffer, self.width, self.height, framebuf.MONO_VLSB)
        self.init_display()

    def init_display(self):
        cmds = [0xAE, 0xD5, 0x80, 0xA8, 0x3F, 0xD3, 0x00, 0x40,
                0x8D, 0x14, 0xA0, 0xC0, 0xDA, 0x12, 0x81, 0xCF,
                0xD9, 0xF1, 0xDB, 0x40, 0xA4, 0xA6, 0xAF]
        for cmd in cmds:
            try:
                self.i2c.writeto_mem(self.addr, 0x00, bytes([cmd]))
            except Exception:
                pass

    def show(self):
        for page in range(self.height // 8):
            try:
                self.i2c.writeto_mem(self.addr, 0x00, bytes([0xB0 + page]))
                self.i2c.writeto_mem(self.addr, 0x00, bytes([0x02]))
                self.i2c.writeto_mem(self.addr, 0x00, bytes([0x10]))
                start = page * self.width
                end = start + self.width
                self.i2c.writeto_mem(self.addr, 0x40, self.buffer[start:end])
            except Exception:
                pass


# --- Hardware Connections ---
i2c_oled = I2C(0, sda=Pin(16), scl=Pin(17), freq=400000)
i2c_mpu  = I2C(1, sda=Pin(23), scl=Pin(22), freq=400000)

oled = None
oled_devices = i2c_oled.scan()
if oled_devices:
    try:
        oled = FixedOLED(128, 64, i2c_oled, addr=oled_devices[0])
    except Exception as e:
        print(f"[!] OLED Init Error: {e}")

mpu_available = False
mpu_addr = 0x68

def init_mpu():
    global mpu_available, mpu_addr
    mpu_devices = i2c_mpu.scan()
    if 0x68 in mpu_devices or 0x69 in mpu_devices:
        mpu_addr = 0x68 if 0x68 in mpu_devices else 0x69
        try:
            i2c_mpu.writeto_mem(mpu_addr, 0x6B, b"\x00")
            time.sleep(0.02)
            i2c_mpu.writeto_mem(mpu_addr, 0x1C, b"\x10")
            time.sleep(0.02)
            mpu_available = True
            return True
        except Exception:
            pass
    mpu_available = False
    return False

init_mpu()

prev_tot_accel = 1.0
mpu_error = False
mpu_fail_count = 0
MPU_FAIL_LIMIT = 20

def read_g():
    data = i2c_mpu.readfrom_mem(mpu_addr, 0x3B, 6)
    ax, ay, az = struct.unpack(">hhh", data)
    if ax == ay == az and ax in (0, -1):
        raise ValueError("bad data")
    return math.sqrt((ax / 4096.0)**2 + (ay / 4096.0)**2 + (az / 4096.0)**2)

def mpu_failed():
    global mpu_error, mpu_fail_count
    mpu_fail_count += 1
    if mpu_fail_count >= MPU_FAIL_LIMIT:
        mpu_error = True
    if mpu_fail_count % 10 == 0:
        init_mpu()

def get_mpu_motion():
    global prev_tot_accel, mpu_error, mpu_fail_count
    if not mpu_available and not init_mpu():
        mpu_failed()
        return "BAD", 1.0

    samples = []
    for _ in range(4):
        try:
            samples.append(read_g())
        except Exception:
            pass
        time.sleep_us(500)

    if len([g for g in samples if g > ALARM_G]) < 2:
        samples = [g for g in samples if g <= 10.0]
    samples = [min(g, 10.0) for g in samples]

    if not samples:
        mpu_failed()
        return "BAD", prev_tot_accel

    mpu_fail_count = 0
    mpu_error = False

    peak = max(samples)
    low = min(samples)
    avg = sum(samples) / len(samples)
    delta = abs(avg - prev_tot_accel)
    prev_tot_accel = avg

    if peak > ALARM_G:
        return "DROP", peak
    if low < FREEFALL_G:
        return "DROP", low
    if avg > 1.40 or delta > 0.20:
        return "MOVE", peak
    return "STILL", avg


ecg_adc = ADC(Pin(34))
lo_minus = Pin(32, Pin.IN)
lo_plus = Pin(35, Pin.IN)

buzzer_pwm = PWM(Pin(18))
buzzer_pwm.freq(2800)
buzzer_pwm.duty_u16(0)

def buzzer_on():
    buzzer_pwm.duty_u16(32768)

def buzzer_off():
    buzzer_pwm.duty_u16(0)

try:
    ecg_adc.atten(ADC.ATTN_11DB)
except Exception:
    pass

def read_adc():
    try:
        return ecg_adc.read_u16()
    except AttributeError:
        return ecg_adc.read() * 16


CHART_TOP = 15
CHART_HEIGHT = 48
CHART_BOTTOM = 63
FLATLINE_Y = CHART_TOP + (CHART_HEIGHT // 2)

min_val = 20000
max_val = 45000
last_peak_time = time.ticks_ms()
realtime_bpm = 0
x_prev = 0
y_prev = FLATLINE_Y
x = 0

crossings = 0
last_cross_check = time.ticks_ms()
last_state_above = False
last_motion_printed = None
last_mpu_check = time.ticks_ms()
MPU_INTERVAL_MS = 0
state_peak = 1.0
shown_g = 1.0
last_mpu_error = False

drop_counter = 0
move_counter = 0
still_counter = 0
debounced_state = "STILL"
drop_start_time = 0
last_raw_value = 32000

BPM_UPDATE_INTERVAL_MS = 10000
BPM_MIN = 45
BPM_MAX = 185
beat_count = 0
interval_sum = 0
bpm_window_start = time.ticks_ms()

if oled:
    oled.fill(0)
    oled.text("ECG MONITOR", 18, 18)
    oled.text("Initialising...", 8, 36)
    oled.show()
    time.sleep(1)
    oled.fill(0)

print("[+] System Loop Running...")

while True:
    loop_start = time.ticks_ms()
    now = loop_start
    raw = read_adc()

    if time.ticks_diff(now, last_mpu_check) >= MPU_INTERVAL_MS:
        raw_state, g_val = get_mpu_motion()

        if raw_state != "BAD":
            if g_val > state_peak:
                state_peak = g_val

            if raw_state == "DROP":
                drop_counter += 1
                move_counter = 0
                still_counter = 0
                if g_val > ALARM_G and not alarm_active and alarm_armed:
                    alarm_active = True
                    alarm_armed = False
                    move_beep_active = False
                    alarm_start = now
                    alarm_g = g_val
                    telegram_pending = True
                    drop_start_time = now
                    debounced_state = "DROP"
                    print(f"[ALARM]: Impact {g_val:.2f}g! Long beep.")
                elif drop_counter >= 2:
                    if debounced_state != "DROP":
                        drop_start_time = now
                    debounced_state = "DROP"
                    if drop_beep_armed and not alarm_active:
                        move_beep_active = True
                        move_beep_start = now
                        beep_len = DROP_BEEP_MS
                        drop_beep_armed = False
                        print("[DROP]: Single beep.")
            elif raw_state == "MOVE":
                move_counter += 1
                drop_counter = 0
                still_counter = 0
                if move_counter >= 2:
                    debounced_state = "MOVE"
                    if move_armed and not alarm_active:
                        move_beep_active = True
                        move_beep_start = now
                        beep_len = MOVE_BEEP_MS
                        move_armed = False
                        print("[MOVE]: Single beep.")
            elif raw_state == "STILL":
                still_counter += 1
                drop_counter = 0
                move_counter = 0
                if still_counter >= 2:
                    debounced_state = "STILL"
                    move_armed = True
                    drop_beep_armed = True
                    if not alarm_active:
                        alarm_armed = True

            if debounced_state == "DROP" and time.ticks_diff(now, drop_start_time) > 2000:
                debounced_state = "STILL"
                drop_counter = 0

            if debounced_state != last_motion_printed:
                shown_g = state_peak
                print(f"[MOTION]: {debounced_state:<6} Peak force: {shown_g:.2f}g")
                last_motion_printed = debounced_state
                state_peak = g_val

        last_mpu_check = now

    if mpu_error != last_mpu_error:
        if mpu_error:
            print("[!] MPU6050 not responding - check wiring")
        else:
            print("[+] MPU6050 reading OK again.")
        last_mpu_error = mpu_error

    if alarm_active:
        if time.ticks_diff(now, alarm_start) < ALARM_DURATION_MS:
            buzzer_on()
        else:
            alarm_active = False
            buzzer_off()
            print("[ALARM]: Finished, buzzer off.")
            if telegram_pending:
                telegram_pending = False
                send_telegram_alert(alarm_g)
    elif move_beep_active:
        if time.ticks_diff(now, move_beep_start) < beep_len:
            buzzer_on()
        else:
            move_beep_active = False
            buzzer_off()
    else:
        buzzer_off()

    hw_leads_off = lo_minus.value() or lo_plus.value()
    sw_saturated = (raw > 61000) or (raw < 2000)

    midpoint = min_val + ((max_val - min_val) // 2)
    current_above = raw > midpoint

    if current_above != last_state_above:
        crossings += 1
        last_state_above = current_above

    is_ac_noise = False
    if time.ticks_diff(now, last_cross_check) > 300:
        if crossings > 12:
            is_ac_noise = True
        crossings = 0
        last_cross_check = now

    if hw_leads_off or sw_saturated or is_ac_noise:
        y = FLATLINE_Y
        if hw_leads_off:
            beat_count = 0
            interval_sum = 0
        min_val = 20000
        max_val = 45000
    else:
        if raw < min_val:
            min_val = raw
        if raw > max_val:
            max_val = raw

        diff = max_val - min_val
        if diff < 3000:
            y = FLATLINE_Y
        else:
            y = CHART_BOTTOM - int(((raw - min_val) / diff) * (CHART_HEIGHT - 1))
            y = max(CHART_TOP, min(CHART_BOTTOM, y))

            if debounced_state == "STILL":
                slope_delta = raw - last_raw_value
                time_since_last_peak = time.ticks_diff(now, last_peak_time)
                slope_trigger_threshold = int(diff * 0.18)

                if slope_delta > slope_trigger_threshold and time_since_last_peak > 350:
                    if 325 <= time_since_last_peak <= 1333:
                        instant_bpm = 60000 / time_since_last_peak
                        if BPM_MIN <= instant_bpm <= BPM_MAX:
                            interval_sum += time_since_last_peak
                            beat_count += 1
                    last_peak_time = now

            if time.ticks_diff(now, bpm_window_start) >= BPM_UPDATE_INTERVAL_MS:
                if beat_count >= 1 and interval_sum > 0:
                    average_interval = interval_sum / beat_count
                    realtime_bpm = int((60000 / average_interval) + 0.5)
                    print(f"[BPM]: {realtime_bpm} BPM")
                else:
                    realtime_bpm = 0
                beat_count = 0
                interval_sum = 0
                bpm_window_start = now

            if time.ticks_diff(now, last_peak_time) > 5000:
                beat_count = 0
                interval_sum = 0

    last_raw_value = raw

    if oled:
        oled.fill_rect(0, 0, 128, CHART_TOP, 0)
        if alarm_active:
            oled.text("DROP!", 0, 2)
        elif realtime_bpm > 0:
            oled.text(f"BPM:{realtime_bpm}", 0, 2)
        else:
            oled.text("BPM:--", 0, 2)

        oled.text("ERR" if mpu_error else f"{shown_g:.1f}g", 80, 2)
        oled.line(0, CHART_TOP - 1, 127, CHART_TOP - 1, 1)

        clear_x = (x + 3) % 128
        oled.fill_rect(clear_x, CHART_TOP, 4, CHART_HEIGHT, 0)
        oled.line(x_prev, y_prev, x, y, 1)
        oled.show()

    x_prev = x
    y_prev = y
    x += 1
    if x >= 128:
        x = 0
        x_prev = 0

    min_val = int(min_val + 2)
    max_val = int(max_val - 2)

    loop_elapsed = time.ticks_diff(time.ticks_ms(), loop_start)
    if loop_elapsed < 15:
        time.sleep_ms(15 - loop_elapsed)
    else:
        time.sleep_ms(1)
