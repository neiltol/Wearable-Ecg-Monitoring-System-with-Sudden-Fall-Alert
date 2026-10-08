# Wearable ECG Monitoring with Sudden Fall Alert

A MicroPython-based IoT safety monitor for ESP32 that combines biopotential heart monitoring (ECG) with real-time motion detection (fall alerts).

## Features
- **Real-Time ECG Processing**: Measures BPM using the AD8232 ECG sensor.
- **Fall Detection**: Uses MPU6050 6-axis accelerometer/gyroscope to detect motion states (`STILL`, `MOVE`, `DROP`).
- **Local Alerts**: 10-second buzzer alarm upon detecting a high-impact fall (`DROP`).
- **Visual Display**: Live ECG waveform and status rendering on a 0.96" I2C OLED display.
- **IoT Telegram Notifications**: Sends automatic emergency alerts with g-force impact metrics to a Telegram bot via ESP32 Wi-Fi.

## Hardware Components
- **Microcontroller**: ESP32 Development Board
- **ECG Sensor**: AD8232 Heart Rate Monitor
- **IMU Sensor**: MPU6050 (Accelerometer + Gyroscope)
- **Display**: 0.96" I2C OLED Display (SSD1306/SH1106)
- **Audio Alert**: Active/Passive Piezoelectric Buzzer

## Pin Mapping
| Module | Module Pin | ESP32 GPIO |
|---|---|---|
| **AD8232 ECG** | OUTPUT | GPIO 34 |
| | LO- | GPIO 32 |
| | LO+ | GPIO 35 |
| **MPU6050 IMU** | SDA | GPIO 23 |
| | SCL | GPIO 22 |
| **I2C OLED** | SDA | GPIO 16 |
| | SCL | GPIO 17 |
| **Buzzer** | Positive (+) | GPIO 18 |

## Setup Instructions
1. Clone this repository.
2. Rename `config.py.example` (or create `config.py`) and enter your Wi-Fi SSID, Password, and Telegram Bot credentials.
3. Flash MicroPython onto your ESP32 board.
4. Upload `config.py` and `main.py` using Thonny IDE.
