import abc
import struct
import unittest
import numpy as np
from opendbc.can.packer import CANPacker # pylint: disable=import-error
from panda.tests.safety import libpandasafety_py

MAX_WRONG_COUNTERS = 5

class UNSAFE_MODE:
  DEFAULT = 0
  DISABLE_DISENGAGE_ON_GAS = 1
  DISABLE_STOCK_AEB = 2
  RAISE_LONGITUDINAL_LIMITS_TO_ISO_MAX = 8

def package_can_msg(msg):
  addr, _, dat, bus = msg
  rdlr, rdhr = struct.unpack('II', dat.ljust(8, b'\x00'))

  ret = libpandasafety_py.ffi.new('CAN_FIFOMailBox_TypeDef *')
  if addr >= 0x800:
    ret[0].RIR = (addr << 3) | 5
  else:
    ret[0].RIR = (addr << 21) | 1
  ret[0].RDTR = len(dat) | ((bus & 0xF) << 4)
  ret[0].RDHR = rdhr
  ret[0].RDLR = rdlr

  return ret

def make_msg(bus, addr, length=8):
  return package_can_msg([addr, 0, b'\x00'*length, bus])

class CANPackerPanda(CANPacker):
  def make_can_msg_panda(self, name_or_addr, bus, values, counter=-1):
    msg = self.make_can_msg(name_or_addr, bus, values, counter=-1)
    return package_can_msg(msg)

class PandaSafetyTestBase(unittest.TestCase):
  @classmethod
  def setUpClass(cls):
    if cls.__name__ == "PandaSafetyTestBase":
      cls.safety = None
      raise unittest.SkipTest

  def _rx(self, msg):
    return self.safety.safety_rx_hook(msg)

  def _tx(self, msg):
    return self.safety.safety_tx_hook(msg)

class InterceptorSafetyTest(PandaSafetyTestBase):

  INTERCEPTOR_THRESHOLD = 0

  @classmethod
  def setUpClass(cls):
    if cls.__name__ == "InterceptorSafetyTest":
      cls.safety = None
      raise unittest.SkipTest

  @abc.abstractmethod
  def _interceptor_msg(self, gas, addr):
    pass

  def test_prev_gas_interceptor(self):
    self._rx(self._interceptor_msg(0x0, 0x201))
    self.assertFalse(self.safety.get_gas_interceptor_prev())
    self._rx(self._interceptor_msg(0x1000, 0x201))
    self.assertTrue(self.safety.get_gas_interceptor_prev())
    self._rx(self._interceptor_msg(0x0, 0x201))
    self.safety.set_gas_interceptor_detected(False)

  def test_disengage_on_gas_interceptor(self):
    for g in range(0, 0x1000):
      self._rx(self._interceptor_msg(0, 0x201))
      self.safety.set_controls_allowed(True)
      self._rx(self._interceptor_msg(g, 0x201))
      remain_enabled = g <= self.INTERCEPTOR_THRESHOLD
      self.assertEqual(remain_enabled, self.safety.get_controls_allowed())
      self._rx(self._interceptor_msg(0, 0x201))
      self.safety.set_gas_interceptor_detected(False)

  def test_unsafe_mode_no_disengage_on_gas_interceptor(self):
    self.safety.set_controls_allowed(True)
    self.safety.set_unsafe_mode(UNSAFE_MODE.DISABLE_DISENGAGE_ON_GAS)
    for g in range(0, 0x1000):
      self._rx(self._interceptor_msg(g, 0x201))
      self.assertTrue(self.safety.get_controls_allowed())
      self._rx(self._interceptor_msg(0, 0x201))
      self.safety.set_gas_interceptor_detected(False)
    self.safety.set_unsafe_mode(UNSAFE_MODE.DEFAULT)

  def test_allow_engage_with_gas_interceptor_pressed(self):
    self._rx(self._interceptor_msg(0x1000, 0x201))
    self.safety.set_controls_allowed(1)
    self._rx(self._interceptor_msg(0x1000, 0x201))
    self.assertTrue(self.safety.get_controls_allowed())
    self._rx(self._interceptor_msg(0, 0x201))

  def test_gas_interceptor_safety_check(self):
    for gas in np.arange(0, 4000, 100):
      for controls_allowed in [True, False]:
        self.safety.set_controls_allowed(controls_allowed)
        if controls_allowed:
          send = True
        else:
          send = gas == 0
        self.assertEqual(send, self._tx(self._interceptor_msg(gas, 0x200)))


class PandaSafetyTest(PandaSafetyTestBase):
  TX_MSGS = None
  STANDSTILL_THRESHOLD = None
  GAS_PRESSED_THRESHOLD = 0
  RELAY_MALFUNCTION_ADDR = None
  RELAY_MALFUNCTION_BUS = None
  FWD_BLACKLISTED_ADDRS = {} # {bus: [addr]}
  FWD_BUS_LOOKUP = {}

  @classmethod
  def setUpClass(cls):
    if cls.__name__ == "PandaSafetyTest":
      cls.safety = None
      raise unittest.SkipTest

  @abc.abstractmethod
  def _brake_msg(self, brake):
    pass

  @abc.abstractmethod
  def _speed_msg(self, speed):
    pass

  @abc.abstractmethod
  def _gas_msg(self, speed):
    pass

  @abc.abstractmethod
  def _pcm_status_msg(self, enable):
    pass

  # ***** standard tests for all safety modes *****

  def test_relay_malfunction(self):
    # each car has an addr that is used to detect relay malfunction
    # if that addr is seen on specified bus, triggers the relay malfunction
    # protection logic: both tx_hook and fwd_hook are expected to return failure
    self.assertFalse(self.safety.get_relay_malfunction())
    self._rx(make_msg(self.RELAY_MALFUNCTION_BUS, self.RELAY_MALFUNCTION_ADDR, 8))
    self.assertTrue(self.safety.get_relay_malfunction())
    for a in range(1, 0x800):
      for b in range(0, 3):
        self.assertFalse(self._tx(make_msg(b, a, 8)))
        self.assertEqual(-1, self.safety.safety_fwd_hook(b, make_msg(b, a, 8)))

  def test_fwd_hook(self):
    # some safety modes don't forward anything, while others blacklist msgs
    for bus in range(0x0, 0x3):
      for addr in range(0x1, 0x800):
        # assume len 8
        msg = make_msg(bus, addr, 8)
        fwd_bus = self.FWD_BUS_LOOKUP.get(bus, -1)
        if bus in self.FWD_BLACKLISTED_ADDRS and addr in self.FWD_BLACKLISTED_ADDRS[bus]:
          fwd_bus = -1
        self.assertEqual(fwd_bus, self.safety.safety_fwd_hook(bus, msg))

  def test_spam_can_buses(self):
    for addr in range(1, 0x800):
      for bus in range(0, 4):
        if all(addr != m[0] or bus != m[1] for m in self.TX_MSGS):
          self.assertFalse(self._tx(make_msg(bus, addr, 8)))

  def test_default_controls_not_allowed(self):
    self.assertFalse(self.safety.get_controls_allowed())

  def test_manually_enable_controls_allowed(self):
    self.safety.set_controls_allowed(1)
    self.assertTrue(self.safety.get_controls_allowed())
    self.safety.set_controls_allowed(0)
    self.assertFalse(self.safety.get_controls_allowed())

  def test_prev_gas(self):
    self.assertFalse(self.safety.get_gas_pressed_prev())
    for pressed in [self.GAS_PRESSED_THRESHOLD+1, 0]:
      self._rx(self._gas_msg(pressed))
      self.assertEqual(bool(pressed), self.safety.get_gas_pressed_prev())

  def test_allow_engage_with_gas_pressed(self):
    self._rx(self._gas_msg(1))
    self.safety.set_controls_allowed(True)
    self._rx(self._gas_msg(1))
    self.assertTrue(self.safety.get_controls_allowed())
    self._rx(self._gas_msg(1))
    self.assertTrue(self.safety.get_controls_allowed())

  def test_disengage_on_gas(self):
    self._rx(self._gas_msg(0))
    self.safety.set_controls_allowed(True)
    self._rx(self._gas_msg(self.GAS_PRESSED_THRESHOLD+1))
    self.assertFalse(self.safety.get_controls_allowed())

  def test_unsafe_mode_no_disengage_on_gas(self):
    self._rx(self._gas_msg(0))
    self.safety.set_controls_allowed(True)
    self.safety.set_unsafe_mode(UNSAFE_MODE.DISABLE_DISENGAGE_ON_GAS)
    self._rx(self._gas_msg(self.GAS_PRESSED_THRESHOLD+1))
    self.assertTrue(self.safety.get_controls_allowed())

  def test_prev_brake(self):
    self.assertFalse(self.safety.get_brake_pressed_prev())
    for pressed in [True, False]:
      self._rx(self._brake_msg(not pressed))
      self.assertEqual(not pressed, self.safety.get_brake_pressed_prev())
      self._rx(self._brake_msg(pressed))
      self.assertEqual(pressed, self.safety.get_brake_pressed_prev())

  def test_enable_control_allowed_from_cruise(self):
    self._rx(self._pcm_status_msg(False))
    self.assertFalse(self.safety.get_controls_allowed())
    self._rx(self._pcm_status_msg(True))
    self.assertTrue(self.safety.get_controls_allowed())

  def test_disable_control_allowed_from_cruise(self):
    self.safety.set_controls_allowed(1)
    self._rx(self._pcm_status_msg(False))
    self.assertFalse(self.safety.get_controls_allowed())

  def test_cruise_engaged_prev(self):
    for engaged in [True, False]:
      self._rx(self._pcm_status_msg(engaged))
      self.assertEqual(engaged, self.safety.get_cruise_engaged_prev())
      self._rx(self._pcm_status_msg(not engaged))
      self.assertEqual(not engaged, self.safety.get_cruise_engaged_prev())

  def test_allow_brake_at_zero_speed(self):
    # Brake was already pressed
    self._rx(self._speed_msg(0))
    self._rx(self._brake_msg(1))
    self.safety.set_controls_allowed(1)
    self._rx(self._brake_msg(1))
    self.assertTrue(self.safety.get_controls_allowed())
    self._rx(self._brake_msg(0))
    self.assertTrue(self.safety.get_controls_allowed())
    # rising edge of brake should disengage
    self._rx(self._brake_msg(1))
    self.assertFalse(self.safety.get_controls_allowed())
    self._rx(self._brake_msg(0))  # reset no brakes

  def test_not_allow_brake_when_moving(self):
    # Brake was already pressed
    self._rx(self._brake_msg(1))
    self.safety.set_controls_allowed(1)
    self._rx(self._speed_msg(self.STANDSTILL_THRESHOLD))
    self._rx(self._brake_msg(1))
    self.assertTrue(self.safety.get_controls_allowed())
    self._rx(self._speed_msg(self.STANDSTILL_THRESHOLD + 1))
    self._rx(self._brake_msg(1))
    self.assertFalse(self.safety.get_controls_allowed())
    self._rx(self._speed_msg(0))

  def test_sample_speed(self):
    self.assertFalse(self.safety.get_vehicle_moving())
    
    # not moving
    self.safety.safety_rx_hook(self._speed_msg(0))
    self.assertFalse(self.safety.get_vehicle_moving())

    # speed is at threshold
    self.safety.safety_rx_hook(self._speed_msg(self.STANDSTILL_THRESHOLD))
    self.assertFalse(self.safety.get_vehicle_moving())

    # past threshold
    self.safety.safety_rx_hook(self._speed_msg(self.STANDSTILL_THRESHOLD+1))
    self.assertTrue(self.safety.get_vehicle_moving())
