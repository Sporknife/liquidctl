"""liquidctl drivers for Corsair Commander Pro devices.

Supported devices:

- Corsair Commander Pro
- Corsair Lighting Node Pro


NOTE:
    This device currently only has hardware control implemented but it also supports a software control mode.
    Software control will be enabled at a future time.


Copyright (C) 2020–2021  Marshall Asch and contributors
SPDX-License-Identifier: GPL-3.0-or-later
"""

import itertools
import logging
import re
from enum import Enum, unique

from liquidctl.driver.usb import UsbHidDriver
from liquidctl.error import NotSupportedByDevice
from liquidctl.keyval import RuntimeStorage
from liquidctl.pmbus import compute_pec
from liquidctl.util import clamp, fraction_of_byte, u16be_from, u16le_from, \
                           normalize_profile, check_unsafe, map_direction

_LOGGER = logging.getLogger(__name__)

_REPORT_LENGTH = 64
_RESPONSE_LENGTH = 16


_CMD_GET_FIRMWARE = 0x02
_CMD_GET_BOOTLOADER = 0x06
_CMD_GET_TEMP_CONFIG = 0x10
_CMD_GET_TEMP = 0x11
_CMD_GET_VOLTS = 0x12
_CMD_GET_FAN_MODES = 0x20
_CMD_GET_FAN_RPM = 0x21
_CMD_SET_FAN_DUTY = 0x23
_CMD_SET_FAN_PROFILE = 0x25

_CMD_RESET_LED_CHANNEL = 0x37
_CMD_BEGIN_LED_EFFECT = 0x34
_CMD_SET_LED_CHANNEL_STATE = 0x38
_CMD_LED_EFFECT = 0x35
_CMD_LED_COMMIT = 0x33

_LED_PORT_STATE_HARDWARE = 0x01
_LED_PORT_STATE_SOFTWARE = 0x02
_LED_SPEED_FAST = 0x00
_LED_SPEED_MEDIUM = 0x01
_LED_SPEED_SLOW = 0x02

_LED_DIRECTION_FORWARD = 0x01
_LED_DIRECTION_BACKWARD = 0x00

_FAN_MODE_DISCONNECTED = 0x00
_FAN_MODE_DC = 0x01
_FAN_MODE_PWM = 0x02


_PROFILE_LENGTH = 6
_CRITICAL_TEMPERATURE = 60
_CRITICAL_TEMPERATURE_HIGH = 100
_MAX_FAN_RPM = 5000             # I have no idea if this is a good value or not

_MODES = {
    'off': 0x04,            # this is a special case of fixed
    'rainbow': 0x00,
    'color_shift': 0x01,
    'color_pulse': 0x02,
    'color_wave': 0x03,
    'fixed': 0x04,
    # 'temperature': 0x05,    # ignore this
    'visor': 0x06,
    'marquee': 0x07,
    'blink': 0x08,
    'sequential': 0x09,
    'rainbow2': 0x0a,
}


def _prepare_profile(original, critcalTempature):
    clamped = ((temp, clamp(duty, 0, _MAX_FAN_RPM)) for temp, duty in original)
    normal = normalize_profile(clamped, critcalTempature, _MAX_FAN_RPM)
    missing = _PROFILE_LENGTH - len(normal)
    if missing < 0:
        raise ValueError(f'too many points in profile (remove {-missing})')
    if missing > 0:
        normal += missing * [(critcalTempature, _MAX_FAN_RPM)]
    return normal


def _quoted(*names):
    return ', '.join(map(repr, names))


def _get_fan_mode_description(mode):
    """This will convert the fan mode value to a descriptive name.
    """

    if mode == _FAN_MODE_DISCONNECTED:
        return 'Auto/Disconnected'
    elif mode == _FAN_MODE_DC:
        return 'DC'
    elif mode == _FAN_MODE_PWM:
        return 'PWM'
    else:
        return 'UNKNOWN'


class CommanderPro(UsbHidDriver):
    """Corsair Commander Pro LED and fan hub"""

    SUPPORTED_DEVICES = [
        (0x1b1c, 0x0c10, None, 'Corsair Commander Pro (experimental)',
            {'fan_count': 6, 'temp_probs': 4, 'led_channels': 2}),
        (0x1b1c, 0x0c0b, None, 'Corsair Lighting Node Pro (experimental)',
            {'fan_count': 0, 'temp_probs': 0, 'led_channels': 2}),
        (0x1b1c, 0x0c1a, None, 'Corsair Lighting Node Core (experimental)',
            {'fan_count': 0, 'temp_probs': 0, 'led_channels': 1}),
    ]

    def __init__(self, device, description, fan_count, temp_probs, led_channels, **kwargs):
        super().__init__(device, description, **kwargs)

        # the following fields are only initialized in connect()
        self._data = None
        self._fan_names = [f'fan{i+1}' for i in range(fan_count)]
        if led_channels == 1:
            self._led_names = ['led']
        else:
            self._led_names = [f'led{i+1}' for i in range(led_channels)]
        self._temp_probs = temp_probs
        self._fan_count = fan_count

    def connect(self, runtime_storage=None, **kwargs):
        """Connect to the device."""
        ret = super().connect(**kwargs)
        if runtime_storage:
            self._data = runtime_storage
        else:
            ids = f'vid{self.vendor_id:04x}_pid{self.product_id:04x}'
            # must use the HID path because there is no serial number; however,
            # these can be quite long on Windows and macOS, so only take the
            # numbers, since they are likely the only parts that vary between two
            # devices of the same model
            loc = 'loc' + '_'.join(re.findall(r'\d+', self.address))
            self._data = RuntimeStorage(key_prefixes=[ids, loc])
        return ret

    def initialize(self, **kwargs):
        """Initialize the device and get the fan modes.

        The device should be initialized every time it is powered on, including when
        the system resumes from suspending to memory.

        Returns a list of `(property, value, unit)` tuples.
        """

        res = self._send_command(_CMD_GET_FIRMWARE)
        fw_version = (res[1], res[2], res[3])

        res = self._send_command(_CMD_GET_BOOTLOADER)
        bootloader_version = (res[1], res[2])               # is it possible for there to be a third value?

        status = [
            ('Firmware version', '{}.{}.{}'.format(*fw_version), ''),
            ('Bootloader version', '{}.{}'.format(*bootloader_version), ''),
        ]

        if self._temp_probs > 0:
            res = self._send_command(_CMD_GET_TEMP_CONFIG)
            temp_connected = res[1:5]
            self._data.store('temp_sensors_connected', temp_connected)
            status += [
                ('Temp sensor 1', 'Connected' if temp_connected[0] else 'Not Connected', ''),
                ('Temp sensor 2', 'Connected' if temp_connected[1] else 'Not Connected', ''),
                ('Temp sensor 3', 'Connected' if temp_connected[2] else 'Not Connected', ''),
                ('Temp sensor 4', 'Connected' if temp_connected[3] else 'Not Connected', ''),
            ]

        if self._fan_count > 0:
            # get the information about how the fans are connected, probably want to save this for later
            res = self._send_command(_CMD_GET_FAN_MODES)
            fanModes = res[1:self._fan_count+1]
            self._data.store('fan_modes', fanModes)
            status += [
                ('Fan 1 Mode', _get_fan_mode_description(fanModes[0]), ''),
                ('Fan 2 Mode', _get_fan_mode_description(fanModes[1]), ''),
                ('Fan 3 Mode', _get_fan_mode_description(fanModes[2]), ''),
                ('Fan 4 Mode', _get_fan_mode_description(fanModes[3]), ''),
                ('Fan 5 Mode', _get_fan_mode_description(fanModes[4]), ''),
                ('Fan 6 Mode', _get_fan_mode_description(fanModes[5]), ''),
            ]

        return status

    def get_status(self, **kwargs):
        """Get a status report.

        Returns a list of `(property, value, unit)` tuples.
        """

        if self.device.product_id != 0x0c10:
            _LOGGER.debug('only the Commander Pro supports this')
            return []

        connected_temp_sensors = self._data.load('temp_sensors_connected', default=[0]*self._temp_probs)
        fan_modes = self._data.load('fan_modes', default=[0]*self._fan_count)

        # get the temperature sensor values
        temp = [0]*self._temp_probs
        for num, enabled in enumerate(connected_temp_sensors):
            if enabled:
                temp[num] = self._get_temp(num)

        # get the real power supply voltages
        res = self._send_command(_CMD_GET_VOLTS, [0])
        volt_12 = u16be_from(res, offset=1) / 1000

        res = self._send_command(_CMD_GET_VOLTS, [1])
        volt_5 = u16be_from(res, offset=1) / 1000

        res = self._send_command(_CMD_GET_VOLTS, [2])
        volt_3 = u16be_from(res, offset=1) / 1000

        # get fan RPMs of connected fans
        fanspeeds = [0]*self._fan_count
        for fan_num, mode in enumerate(fan_modes):
            if mode == _FAN_MODE_DC or mode == _FAN_MODE_PWM:
                fanspeeds[fan_num] = self._get_fan_rpm(fan_num)

        status = [
            ('12 volt rail', volt_12, 'V'),
            ('5 volt rail', volt_5, 'V'),
            ('3.3 volt rail', volt_3, 'V'),
        ]

        for temp_num in range(self._temp_probs):
            status += [(f'Temp sensor {temp_num + 1}', temp[temp_num], '°C')]

        for fan_num in range(self._fan_count):
            status += [(f'Fan {fan_num + 1} speed', fanspeeds[fan_num], 'rpm')]

        return status

    def _get_temp(self, sensor_num):
        """This will get the temperature in degrees celsius for the specified temp sensor.

        sensor number MUST be in range of 0-3
        """

        if self._temp_probs == 0:
            raise ValueError('this device does not have a temperature sensor')

        if sensor_num < 0 or sensor_num > 3:
            raise ValueError(f'sensor_num {sensor_num} invalid, must be between 0 and 3')

        res = self._send_command(_CMD_GET_TEMP, [sensor_num])
        temp = u16be_from(res, offset=1) / 100

        return temp

    def _get_fan_rpm(self, fan_num):
        """This will get the rpm value of the fan.

        fan number MUST be in range of 0-5
        """

        if self._fan_count == 0:
            raise ValueError('this device does not have any fans')

        if fan_num < 0 or fan_num > 5:
            raise ValueError(f'fan_num {fan_num} invalid, must be between 0 and 5')

        res = self._send_command(_CMD_GET_FAN_RPM, [fan_num])
        speed = u16be_from(res, offset=1)

        return speed

    def _get_hw_fan_channels(self, channel):
        """This will get a list of all the fan channels that the command should be sent to
        It will look up the name of the fan channel given and return a list of the real fan number
        """
        if channel == 'sync':
            return list(range(len(self._fan_names)))
        elif channel in self._fan_names:
            return [self._fan_names.index(channel)]
        elif len(self._fan_names) > 1:
            raise ValueError(f'unknown channel, should be one of: {_quoted("sync", *self._fan_names)}')

    def _get_hw_led_channels(self, channel):
        """This will get a list of all the led channels that the command should be sent to
        It will look up the name of the led channel given and return a list of the real led device number
        """
        if channel == 'sync':
            return list(range(len(self._led_names)))
        elif channel in self._led_names:
            return [self._led_names.index(channel)]
        elif len(self._led_names) > 1:
            raise ValueError(f'unknown channel, should be one of: {_quoted("sync", *self._led_names)}')

    def set_fixed_speed(self, channel, duty, **kwargs):
        """Set fan or fans to a fixed speed duty.

        Valid channel values are 'fanN', where N >= 1 is the fan number, and
        'fan', to simultaneously configure all fans.  Unconfigured fan channels
        may default to 100% duty.

        Different commands for sending fixed percent (0x23) and fixed rpm (0x24)
        Probably want to use fixed percent for this untill the rpm flag is enabled.
        Can only send one fan command at a time, if fan mode is unset will need to send 6?
        messages (or 1 per enabled fan)
        """

        if self._fan_count == 0:
            raise NotSupportedByDevice()

        duty = clamp(duty, 0, 100)
        fan_channels = self._get_hw_fan_channels(channel)
        fan_modes = self._data.load('fan_modes', default=[0]*self._fan_count)

        for fan in fan_channels:
            mode = fan_modes[fan]
            if mode == _FAN_MODE_DC or mode == _FAN_MODE_PWM:
                self._send_command(_CMD_SET_FAN_DUTY, [fan, duty])

    def set_speed_profile(self, channel, profile, temperature_sensor=1, **kwargs):
        """Set fan or fans to follow a speed duty profile.

        Valid channel values are 'fanN', where N >= 1 is the fan number, and
        'fan', to simultaneously configure all fans.  Unconfigured fan channels
        may default to 100% duty.

        Up to six (temperature, duty) pairs can be supplied in `profile`,
        with temperatures in Celsius and duty values in percentage.  The last
        point should set the fan to 100% duty cycle, or be omitted; in the
        latter case the fan will be set to max out at 60°C.
        """

        # send fan num, temp sensor, check to make sure it is actually enabled, and do not let the user send external sensor
        # 6 2-byte big endian temps (celsius * 100), then 6 2-byte big endian rpms
        # need to figure out how to find out what the max rpm is for the given fan

        if self._fan_count == 0:
            raise NotSupportedByDevice()

        profile = list(profile)

        criticalTemp = _CRITICAL_TEMPERATURE_HIGH if check_unsafe('high_temperature', **kwargs) else _CRITICAL_TEMPERATURE
        profile = _prepare_profile(profile, criticalTemp)

        # fan_type = kwargs['fan_type'] # need to make sure this is set
        temp_sensor = clamp(temperature_sensor, 1, self._temp_probs)

        sensors = self._data.load('temp_sensors_connected', default=[0]*self._temp_probs)

        if sensors[temp_sensor-1] != 1:
            raise ValueError('the specified temperature sensor is not connected')

        buf = bytearray(26)
        buf[1] = temp_sensor-1  # 0  # use temp sensor 1

        for i, entry in enumerate(profile):
            temp = entry[0]*100
            rpm = entry[1]

            # convert both values to 2 byte big endian values
            buf[2 + i*2] = temp.to_bytes(2, byteorder='big')[0]
            buf[3 + i*2] = temp.to_bytes(2, byteorder='big')[1]
            buf[14 + i*2] = rpm.to_bytes(2, byteorder='big')[0]
            buf[15 + i*2] = rpm.to_bytes(2, byteorder='big')[1]

        fan_channels = self._get_hw_fan_channels(channel)
        fan_modes = self._data.load('fan_modes', default=[0]*self._fan_count)

        for fan in fan_channels:
            mode = fan_modes[fan]
            if mode == _FAN_MODE_DC or mode == _FAN_MODE_PWM:
                buf[0] = fan
                self._send_command(_CMD_SET_FAN_PROFILE, buf)

    def set_color(self, channel, mode, colors, direction='forward',
                  speed='medium', start_led=1, maximum_leds=1, **kwargs):
        """Set the color of each LED.

        The table bellow summarizes the available channels, modes, and their
        associated maximum number of colors for each device family.

        | Channel  | Mode        | Num colors |
        | -------- | ----------- | ---------- |
        | led      | off         |          0 |
        | led      | fixed       |          1 |
        | led      | color_shift |          2 |
        | led      | color_pulse |          2 |
        | led      | color_wave  |          2 |
        | led      | visor       |          2 |
        | led      | blink       |          2 |
        | led      | marquee     |          1 |
        | led      | sequential  |          1 |
        | led      | rainbow     |          0 |
        | led      | rainbow2    |          0 |
        """

        # a special mode to clear the current led settings.
        # this is usefull if the the user wants to use a led mode for multiple devices
        if mode == 'clear':
            self._data.store('saved_effects', None)
            return

        colors = list(colors)
        expanded = colors[:3]
        c = itertools.chain(*((r, g, b) for r, g, b in expanded))
        colors = list(c)

        direction = map_direction(direction, _LED_DIRECTION_FORWARD, _LED_DIRECTION_BACKWARD)
        speed = _LED_SPEED_SLOW if speed == 'slow' else _LED_SPEED_FAST if speed == 'fast' else _LED_SPEED_MEDIUM
        start_led = clamp(start_led, 1, 204) - 1
        num_leds = clamp(maximum_leds, 1, 204 - start_led - 1)
        random_colors = 0x00 if mode == 'off' or len(colors) != 0 else 0x01
        mode_val = _MODES.get(mode, -1)

        if mode_val == -1:
            raise ValueError(f'mode "{mode}" is not valid')

        # FIXME clears on 'off', while the docs only mention this behavior for 'clear'
        saved_effects = [] if mode == 'off' else self._data.load('saved_effects', default=[])

        for led_channel in self._get_hw_led_channels(channel):

            lighting_effect = {
                    'channel': led_channel,
                    'start_led': start_led,
                    'num_leds': num_leds,
                    'mode': mode_val,
                    'speed': speed,
                    'direction': direction,
                    'random_colors': random_colors,
                    'colors': colors
                }

            saved_effects += [lighting_effect]

            # check to make sure that too many LED effects are not being sent.
            # the max seems to be 8 as found here https://github.com/liquidctl/liquidctl/issues/154#issuecomment-762372583
            if len(saved_effects) > 8:
                _LOGGER.warning(f'too many lighting effects. Run `liquidctl set {channel} color clear` to reset the effect')
                return

            # start sending the led commands
            self._send_command(_CMD_RESET_LED_CHANNEL, [led_channel])
            self._send_command(_CMD_BEGIN_LED_EFFECT, [led_channel])
            self._send_command(_CMD_SET_LED_CHANNEL_STATE, [led_channel, 0x01])

        # FIXME clears on 'off', while the docs only mention this behavior for 'clear'
        self._data.store('saved_effects', None if mode == 'off' else saved_effects)

        for effect in saved_effects:
            config = [effect.get('channel'),
                      effect.get('start_led'),
                      effect.get('num_leds'),
                      effect.get('mode'),
                      effect.get('speed'),
                      effect.get('direction'),
                      effect.get('random_colors'),
                      0xff
                      ] + effect.get('colors')
            self._send_command(_CMD_LED_EFFECT, config)

        self._send_command(_CMD_LED_COMMIT, [0xff])

    def _send_command(self, command, data=None):
        # self.device.write expects buf[0] to be the report number or 0 if not used
        buf = bytearray(_REPORT_LENGTH + 1)
        buf[1] = command
        start_at = 2

        if data:
            data = data[:_REPORT_LENGTH-1]
            buf[start_at: start_at + len(data)] = data

        self.device.clear_enqueued_reports()
        self.device.write(buf)
        buf = bytes(self.device.read(_RESPONSE_LENGTH))
        return buf
