"""
servo_stepper_gui.py

Basic Tkinter GUI to test the STM32 UART Command_t protocol over a
USB-to-TTL adapter.

Packet format (must match uart_comm.h exactly, 9 bytes total, little-endian):

    start_byte  : uint8_t   = 0xAA
    cmd_type    : uint8_t   = 1 (servo) or 2 (stepper)
    sub_cmd     : uint8_t   = see below
    val         : uint16_t  = angle (servo, 0-120) or step count (stepper)
    crc16       : uint16_t  = CRC-16/CCITT-FALSE over the 5 bytes above
    end_byte    : uint8_t   = 0x55

cmd_type = 1 (servo), sub_cmd is 1-indexed:
    1 = servo1, 2 = servo2, 3 = servo3          (val = angle, 0-120)

cmd_type = 2 (stepper), sub_cmd is 0-indexed:
    0 = motor1 backward   1 = motor1 forward
    2 = motor2 backward   3 = motor2 forward     (val = step count)

Requires: pip install pyserial
"""

import struct
import tkinter as tk
from tkinter import ttk, messagebox

import serial
import serial.tools.list_ports

# --------------------------------------------------------------------------
# Protocol constants - keep these in sync with uart_comm.h on the STM32 side
# --------------------------------------------------------------------------
FRAME_START = 0xAA
FRAME_END = 0x55

CMD_SERVO = 1
CMD_STEPPER = 2

SERVO_1, SERVO_2, SERVO_3 = 1, 2, 3  # 1-indexed, matches ServoSubCmd_t

STEP1_BACKWARD = 0
STEP1_FORWARD = 1
STEP2_BACKWARD = 2
STEP2_FORWARD = 3

BAUD_RATE = 115200

# Struct format: little-endian, no padding (matches #pragma pack(push,1)
# on the STM32 side, whose Cortex-M core is little-endian).
#   B = start_byte    B = cmd_type    B = sub_cmd
#   H = val           H = crc16       B = end_byte
_PACKET_FORMAT = "<BBBHHB"


def crc16_ccitt_false(data: bytes) -> int:
    """
    CRC-16/CCITT-FALSE, poly 0x1021, init 0x0000, no reflection, no final
    xor. Must produce IDENTICAL output to CRC16_Calculate() in uart_comm.c
    for a given byte sequence, or every frame gets silently dropped by the
    STM32's CRC check.
    """
    crc = 0x0000
    for byte in data:
        crc ^= (byte << 8) & 0xFFFF
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def build_packet(cmd_type: int, sub_cmd: int, val: int) -> bytes:
    """
    Assembles a full 9-byte Command_t frame ready to write to the serial
    port. CRC is computed over start_byte..val (the first 5 bytes) only -
    it can't cover itself or end_byte.
    """
    # Pack start_byte..val first (5 bytes) so we can CRC exactly that slice.
    head = struct.pack("<BBBH", FRAME_START, cmd_type, sub_cmd, val)
    crc = crc16_ccitt_false(head)
    return struct.pack(_PACKET_FORMAT, FRAME_START, cmd_type, sub_cmd, val, crc, FRAME_END)


class SerialLink:
    """Thin wrapper so the GUI code doesn't need to check `is None` everywhere."""

    def __init__(self):
        self.ser: serial.Serial | None = None

    @property
    def is_open(self) -> bool:
        return self.ser is not None and self.ser.is_open

    def open(self, port: str):
        self.ser = serial.Serial(port, BAUD_RATE, timeout=1)

    def close(self):
        if self.ser is not None:
            self.ser.close()
        self.ser = None

    def send(self, cmd_type: int, sub_cmd: int, val: int):
        if not self.is_open:
            raise RuntimeError("Port not open")
        packet = build_packet(cmd_type, sub_cmd, val)
        self.ser.write(packet)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("STM32 Servo/Stepper UART Tester")
        self.resizable(False, False)

        self.link = SerialLink()

        self._build_connection_row()
        self._build_servo_section()
        self._build_stepper_section()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---- Connection row ---------------------------------------------------
    def _build_connection_row(self):
        frame = ttk.Frame(self, padding=10)
        frame.grid(row=0, column=0, sticky="ew")

        ttk.Label(frame, text="Port:").grid(row=0, column=0, padx=(0, 5))

        self.port_var = tk.StringVar()
        self.port_combo = ttk.Combobox(frame, textvariable=self.port_var, width=15, state="readonly")
        self.port_combo.grid(row=0, column=1, padx=(0, 5))
        self._refresh_ports()

        ttk.Button(frame, text="Refresh", command=self._refresh_ports).grid(row=0, column=2, padx=(0, 10))

        self.connect_btn = ttk.Button(frame, text="Connect", command=self._toggle_connection)
        self.connect_btn.grid(row=0, column=3, padx=(0, 10))

        self.status_var = tk.StringVar(value="Disconnected")
        ttk.Label(frame, textvariable=self.status_var, foreground="red").grid(row=0, column=4)

    def _refresh_ports(self):
        ports = [p.device for p in serial.tools.list_ports.comports()]
        self.port_combo["values"] = ports
        if ports and not self.port_var.get():
            self.port_var.set(ports[0])

    def _toggle_connection(self):
        if self.link.is_open:
            self.link.close()
            self.status_var.set("Disconnected")
            self.connect_btn.config(text="Connect")
        else:
            port = self.port_var.get()
            if not port:
                messagebox.showwarning("No port", "Select a serial port first.")
                return
            try:
                self.link.open(port)
            except Exception as exc:
                messagebox.showerror("Connection failed", str(exc))
                return
            self.status_var.set(f"Connected ({port})")
            self.connect_btn.config(text="Disconnect")

    def _on_close(self):
        self.link.close()
        self.destroy()

    # ---- Servo section ------------------------------------------------------
    def _build_servo_section(self):
        frame = ttk.LabelFrame(self, text="Servos (0-120 deg)", padding=10)
        frame.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 10))

        self.servo_vars = {}
        for sub_cmd, label in ((SERVO_1, "Servo 1"), (SERVO_2, "Servo 2"), (SERVO_3, "Servo 3")):
            row = sub_cmd - 1
            ttk.Label(frame, text=label, width=10).grid(row=row, column=0, sticky="w")

            value_var = tk.IntVar(value=0)
            self.servo_vars[sub_cmd] = value_var

            value_label = ttk.Label(frame, textvariable=value_var, width=4)
            value_label.grid(row=row, column=2, padx=(5, 0))

            scale = ttk.Scale(
                frame, from_=0, to=120, orient="horizontal", length=250,
                command=lambda v, var=value_var: var.set(round(float(v))),
            )
            scale.grid(row=row, column=1, pady=4)
            # Only send on release, not on every pixel of drag - otherwise
            # you'd flood the UART with a packet per mouse-move event.
            scale.bind("<ButtonRelease-1>", lambda evt, sc=sub_cmd, var=value_var: self._send_servo(sc, var))

    def _send_servo(self, sub_cmd: int, value_var: tk.IntVar):
        if not self.link.is_open:
            messagebox.showwarning("Not connected", "Connect to a serial port first.")
            return
        try:
            self.link.send(CMD_SERVO, sub_cmd, value_var.get())
        except Exception as exc:
            messagebox.showerror("Send failed", str(exc))

    # ---- Stepper section ------------------------------------------------------
    def _build_stepper_section(self):
        frame = ttk.LabelFrame(self, text="Steppers", padding=10)
        frame.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 10))

        self.step_entries = {}
        motors = (
            (1, STEP1_BACKWARD, STEP1_FORWARD),
            (2, STEP2_BACKWARD, STEP2_FORWARD),
        )
        for row, (motor_num, backward_cmd, forward_cmd) in enumerate(motors):
            ttk.Label(frame, text=f"Motor {motor_num}", width=10).grid(row=row, column=0, sticky="w")

            entry = ttk.Entry(frame, width=8)
            entry.insert(0, "200")
            entry.grid(row=row, column=1, padx=5)
            self.step_entries[motor_num] = entry

            ttk.Label(frame, text="steps").grid(row=row, column=2, padx=(0, 10))

            ttk.Button(
                frame, text="\u25b2 Forward", width=10,
                command=lambda e=entry, sc=forward_cmd: self._send_stepper(sc, e),
            ).grid(row=row, column=3, padx=2)

            ttk.Button(
                frame, text="\u25bc Backward", width=10,
                command=lambda e=entry, sc=backward_cmd: self._send_stepper(sc, e),
            ).grid(row=row, column=4, padx=2)

    def _send_stepper(self, sub_cmd: int, entry: ttk.Entry):
        if not self.link.is_open:
            messagebox.showwarning("Not connected", "Connect to a serial port first.")
            return
        try:
            steps = int(entry.get())
        except ValueError:
            messagebox.showerror("Invalid input", "Step count must be a whole number.")
            return
        if steps < 0 or steps > 0xFFFF:
            messagebox.showerror("Invalid input", "Step count must fit in 0-65535 (uint16_t).")
            return
        try:
            self.link.send(CMD_STEPPER, sub_cmd, steps)
        except Exception as exc:
            messagebox.showerror("Send failed", str(exc))


if __name__ == "__main__":
    app = App()
    app.mainloop()