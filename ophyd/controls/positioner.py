# vi: ts=4 sw=4
'''
:mod:`ophyd.control.positioner` - Ophyd positioners
===================================================

.. module:: ophyd.control.positioner
   :synopsis:
'''

from __future__ import print_function
import logging
import time

from epics.pv import fmt_time

from .signal import (EpicsSignal, EpicsSignalRO)
from ..utils import TimeoutError, DisconnectedError
from ..utils.epics_pvs import raise_if_disconnected
from .ophydobj import MoveStatus
from .device import (OphydDevice, Component as C)
logger = logging.getLogger(__name__)


class Positioner(OphydDevice):
    '''A soft positioner.

    Subclass from this to implement your own positioners.
    '''

    SUB_START = 'start_moving'
    SUB_DONE = 'done_moving'
    SUB_READBACK = 'readback'
    _SUB_REQ_DONE = '_req_done'  # requested move finished subscription
    _default_sub = SUB_READBACK

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._started_moving = False
        self._moving = False
        self._default_sub = None
        self._position = None
        self._timeout = kwargs.get('timeout', 0.0)
        self._egu = kwargs.get('egu', '')

    @property
    def egu(self):
        return self._egu

    @property
    def limits(self):
        return (0, 0)

    @property
    def low_limit(self):
        return self.limits[0]

    @property
    def high_limit(self):
        return self.limits[1]

    def move(self, position, wait=True,
             moved_cb=None, timeout=30.0):
        '''Move to a specified position, optionally waiting for motion to
        complete.

        Parameters
        ----------
        position
            Position to move to
        wait : bool
            Wait for move completion
        moved_cb : callable
            Call this callback when movement has finished (not applicable if
            `wait` is set)
        timeout : float
            Timeout in seconds

        Raises
        ------
        TimeoutError, ValueError (on invalid positions)
        '''
        self._run_subs(sub_type=self._SUB_REQ_DONE, success=False)
        self._reset_sub(self._SUB_REQ_DONE)

        status = MoveStatus(self, position)
        if wait:
            t0 = time.time()

            def check_timeout():
                return timeout is not None and (time.time() - t0) > timeout

            while not self._started_moving:
                time.sleep(0.05)

                if check_timeout():
                    raise TimeoutError('Failed to move %s to %s '
                                       'in %s s (no motion)' %
                                       (self, position, timeout))

            while self.moving:
                time.sleep(0.05)

                if check_timeout():
                    raise TimeoutError('Failed to move %s to %s in %s s' %
                                       (self, position, timeout))

            status._finished()

        else:
            if moved_cb is not None:
                self.subscribe(moved_cb, event_type=self._SUB_REQ_DONE,
                               run=False)

            self.subscribe(status._finished,
                           event_type=self._SUB_REQ_DONE, run=False)

        return status

    def _done_moving(self, timestamp=None, value=None, **kwargs):
        '''Call when motion has completed.  Runs SUB_DONE subscription.'''

        self._run_subs(sub_type=self.SUB_DONE, timestamp=timestamp,
                       value=value, **kwargs)

        self._run_subs(sub_type=self._SUB_REQ_DONE, timestamp=timestamp,
                       value=value, success=True,
                       **kwargs)
        self._reset_sub(self._SUB_REQ_DONE)

    def stop(self):
        '''Stops motion'''

        self._run_subs(sub_type=self._SUB_REQ_DONE, success=False)
        self._reset_sub(self._SUB_REQ_DONE)

    @property
    @raise_if_disconnected
    def position(self):
        '''The current position of the motor in its engineering units

        Returns
        -------
        position : float
        '''
        return self._position

    def _set_position(self, value, **kwargs):
        '''Set the current internal position, run the readback subscription'''
        self._position = value

        timestamp = kwargs.pop('timestamp', time.time())
        self._run_subs(sub_type=self.SUB_READBACK, timestamp=timestamp,
                       value=value, **kwargs)

    @property
    def moving(self):
        '''Whether or not the motor is moving

        Returns
        -------
        moving : bool
        '''
        return self._moving

    def set(self, new_position, *, wait=False,
            moved_cb=None, timeout=30.0):
        """
        New API for controlling movers.


        Parameters
        ----------
        new_position : dict
            A dictionary of new positions keyed on axes name.  This is
            symmetric with read such that `mot.set(mot.read())` works as
            as expected.
        """
        return self.move(new_position, wait=wait, moved_cb=moved_cb, timeout=timeout)


class EpicsMotor(Positioner):
    '''An EPICS motor record, wrapped in a :class:`Positioner`

    Keyword arguments are passed through to the base class, Positioner

    Parameters
    ----------
    record : str
        The record to use
    '''
    user_readback = C(EpicsSignalRO, '.RBV')
    user_setpoint = C(EpicsSignal, '.VAL', limits=True)
    motor_egu = C(EpicsSignal, '.EGU')
    _is_moving = C(EpicsSignalRO, '.MOVN')
    _done_move = C(EpicsSignalRO, '.DMOV')
    _stop = C(EpicsSignal, '.STOP')

    def __init__(self, record, settle_time=0.05, read_signals=None, name=None):
        if read_signals is None:
            read_signals = ['user_readback', 'user_setpoint', 'motor_egu']

        super().__init__(record, read_signals=read_signals, name=name)

        self.settle_time = float(settle_time)

        self._done_move.subscribe(self._move_changed)
        self.user_readback.subscribe(self._pos_changed)

    @property
    @raise_if_disconnected
    def precision(self):
        '''The precision of the readback PV, as reported by EPICS'''
        return self.user_readback.precision

    @property
    @raise_if_disconnected
    def egu(self):
        '''Engineering units'''
        return self.motor_egu.get()

    @property
    @raise_if_disconnected
    def limits(self):
        return self.user_setpoint.limits

    @property
    @raise_if_disconnected
    def moving(self):
        '''Whether or not the motor is moving

        Returns
        -------
        moving : bool
        '''
        return bool(self._is_moving.get(use_monitor=False))

    @raise_if_disconnected
    def stop(self):
        self._stop.put(1, wait=False)
        Positioner.stop(self)

    @raise_if_disconnected
    def move(self, position, wait=True,
             **kwargs):

        self._started_moving = False

        try:
            self.user_setpoint.put(position, wait=wait)

            return Positioner.move(self, position, wait=wait,
                                   **kwargs)
        except KeyboardInterrupt:
            self.stop()
            raise

    def check_value(self, pos):
        '''Check that the position is within the soft limits'''
        self.user_setpoint.check_value(pos)

    def _pos_changed(self, timestamp=None, value=None,
                     **kwargs):
        '''Callback from EPICS, indicating a change in position'''
        self._set_position(value)

    def _move_changed(self, timestamp=None, value=None, sub_type=None,
                      **kwargs):
        '''Callback from EPICS, indicating that movement status has changed'''
        was_moving = self._moving
        self._moving = (value != 1)

        started = False
        if not self._started_moving:
            started = self._started_moving = (not was_moving and self._moving)

        logger.debug('[ts=%s] %s moving: %s (value=%s)'
                     % (fmt_time(timestamp), self, self._moving, value))

        if started:
            self._run_subs(sub_type=self.SUB_START, timestamp=timestamp,
                           value=value, **kwargs)

        if was_moving and not self._moving:
            self._done_moving(timestamp=timestamp, value=value)

    @property
    def report(self):
        try:
            position = self.position
        except DisconnectedError:
            position = 'disconnected'

        return {self._name: position,
                'pv': self.user_readback.pvname}


# TODO: make Signal aliases uniform between EpicsMotor and PVPositioner
class PVPositioner(Positioner):
    '''A :class:`Positioner`, comprised of multiple :class:`EpicsSignal`s.

    Keyword arguments are passed through to the base class, Positioner

    Parameters
    ----------
    setpoint : str
        The setpoint (request) PV
    readback : str, optional
        The readback PV (e.g., encoder position PV)
    act : str, optional
        The actuation PV to set when movement is requested
    act_val : any, optional
        The actuation value
    stop : str, optional
        The stop PV to set when motion should be stopped
    stop_val : any, optional
        The stop value
    done : str
        A readback value indicating whether motion is finished
    done_val : any, optional
        The value of the done pv when motion has completed
    put_complete : bool, optional
        If set, the specified PV should allow for asynchronous put completion to
        indicate motion has finished.  If `act` is specified, it will be used
        for put completion.  Otherwise, the `setpoint` will be used.  See the
        `-c` option from `caput` for more information.
    settle_time : float, optional
        Time to wait after a move to ensure a move complete callback is received
    limits : 2-element sequence, optional
        (low_limit, high_limit)
    '''

    def __init__(self, setpoint, readback=None,
                 act=None, act_val=1,
                 stop=None, stop_val=1,
                 done=None, done_val=1,
                 put_complete=False,
                 settle_time=0.05,
                 limits=None,
                 **kwargs):

        Positioner.__init__(self, **kwargs)

        self._stop_val = stop_val
        self._done_val = done_val
        self._act_val = act_val
        self._put_complete = bool(put_complete)
        self.settle_time = float(settle_time)

        self._actuate = None
        self._stop = None
        self._done = None

        if limits is None:
            self._limits = (0, 0)
        else:
            self._limits = tuple(limits)

        signals = []
        self.add_signal(EpicsSignal(setpoint, alias='_setpoint', limits=True,
                                    recordable=False))

        if readback is not None:
            self.add_signal(EpicsSignal(readback, alias='_readback',
                                        name=self.name))

            self._readback.subscribe(self._pos_changed)
        else:
            self._setpoint.subscribe(self._pos_changed)

        if act is not None:
            self.add_signal(EpicsSignal(act, alias='_actuate',
                                        recordable=False))

        if stop is not None:
            self.add_signal(EpicsSignal(stop, alias='_stop',
                                        recordable=False))

        if done is None and not self._put_complete:
            msg = '''Positioner %s is mis-configured. A "done" Signal must be
                     provided or put_complete must be True.''' % self.name
            raise ValueError(msg)

        if done is not None:
            self.add_signal(EpicsSignal(done, alias='_done',
                                        recordable=False))

            self._done.subscribe(self._move_changed)
        else:
            self._done_val = False

        for signal in signals:
            self.add_signal(signal)

    def check_value(self, pos):
        '''Check that the position is within the soft limits'''
        self._setpoint.check_value(pos)

    @property
    @raise_if_disconnected
    def moving(self):
        '''Whether or not the motor is moving

        If a `done` PV is specified, it will be read directly to get the motion
        status. If not, it determined from the internal state of PVPositioner.

        Returns
        -------
        bool
        '''
        if self._done is not None:
            dval = self._done.get(use_monitor=False)
            return (dval != self._done_val)
        else:
            return self._moving

    def _move_wait_pc(self, position, **kwargs):
        '''*put complete* Move and wait until motion has completed'''
        has_done = self._done is not None
        if not has_done:
            self._move_changed(value=False)
            self._move_changed(value=True)

        timeout = kwargs.pop('timeout', self._timeout)
        if timeout <= 0.0:
            # TODO pyepics timeout of 0 and None don't mean infinite wait?
            timeout = 1e6

        if self._actuate is None:
            self._setpoint.put(position, wait=True,
                               timeout=timeout)
        else:
            self._setpoint.put(position, wait=False)
            self._actuate.put(self._act_val, wait=True,
                              timeout=timeout)

        if not has_done:
            self._move_changed(value=False)
        else:
            # Does this ever get called? Bluesky will take care of this
            # itself, so this can probably go away. - TAC & DBA
            time.sleep(self.settle_time)

        if self._started_moving and not self._moving:
            self._done_moving(timestamp=self._setpoint.timestamp)
        elif self._started_moving and self._moving:
            # TODO better exceptions
            raise TimeoutError('Failed to move %s to %s'
                               '(put complete done, still moving)' %
                               (self, position))
        else:
            raise TimeoutError('Failed to move %s to %s'
                               '(no motion, put complete)' %
                               (self, position))

    def _move_wait(self, position, **kwargs):
        '''Move and wait until motion has completed'''
        self._started_moving = False

        if self._put_complete:
            self._move_wait_pc(position, **kwargs)
        else:
            self._setpoint.put(position, wait=True)
            logger.debug('Setpoint set: %s = %s' %
                         (self._setpoint.setpoint_pvname, position))

            if self._actuate is not None:
                self._actuate.put(self._act_val, wait=True)
                logger.debug('Actuating: %s = %s'
                             % (self._actuate.setpoint_pvname, self._act_val))

    def _move_async(self, position, **kwargs):
        '''Move and do not wait until motion is complete (asynchronous)'''
        self._started_moving = False

        def done_moving(**kwargs):
            if self._put_complete:
                logger.debug('[%s] Async motion done' % self)
                self._done_moving()

        if self._done is None and self._put_complete:
            # No done signal, so we rely on put completion
            self._move_changed(value=True)

        if self._actuate is not None:
            self._setpoint.put(position, wait=False)
            self._actuate.put(self._act_val, wait=False,
                              callback=done_moving)
        else:
            self._setpoint.put(position, wait=False,
                               callback=done_moving)

    @raise_if_disconnected
    def move(self, position, wait=True, **kwargs):
        if wait:
            try:
                self._move_wait(position, **kwargs)
                return Positioner.move(self, position, wait=True, **kwargs)
            except KeyboardInterrupt:
                self.stop()

        else:
            try:
                # Setup the async retval first
                ret = Positioner.move(self, position, wait=False, **kwargs)

                self._move_async(position, **kwargs)
                return ret
            except KeyboardInterrupt:
                self.stop()

    def _move_changed(self, timestamp=None, value=None, sub_type=None,
                      **kwargs):
        was_moving = self._moving
        self._moving = (value != self._done_val)

        started = False
        if not self._started_moving:
            started = self._started_moving = (not was_moving and self._moving)

        logger.debug('[ts=%s] %s moving: %s (value=%s)' %
                     (fmt_time(timestamp), self, self._moving, value))

        if started:
            self._run_subs(sub_type=self.SUB_START, timestamp=timestamp,
                           value=value, **kwargs)

        if not self._put_complete:
            # In the case of put completion, motion complete
            if was_moving and not self._moving:
                self._done_moving(timestamp=timestamp, value=value)

    def _pos_changed(self, timestamp=None, value=None,
                     **kwargs):
        '''Callback from EPICS, indicating a change in position'''
        self._set_position(value)

    def stop(self):
        self._stop.put(self._stop_val, wait=False)

        Positioner.stop(self)

    # TODO: this will fail if no readback is provided to initializer
    @property
    def report(self):
        return {self._name: self.position, 'pv': self._readback.pvname}

    @property
    def limits(self):
        return tuple(self._limits)

    def __repr__(self):
        repr = ['setpoint={0._setpoint.pvname!r}'.format(self)]
        if self._readback:
            repr.append('readback={0._readback.pvname!r}'.format(self))
        if self._actuate:
            repr.append('act={0._actuate.pvname!r}'.format(self))
            repr.append('act_val={0._act_val!r}'.format(self))
        if self._stop:
            repr.append('stop={0._stop.pvname!r}'.format(self))
            repr.append('stop_val={0._stop_val!r}'.format(self))
        if self._done:
            repr.append('done={0._done.pvname!r}'.format(self))
            repr.append('done_val={0._done_val!r}'.format(self))
        repr.append('put_complete={0._put_complete!r}'.format(self))
        repr.append('settle_time={0.settle_time!r}'.format(self))
        repr.append('limits={0._limits!r}'.format(self))

        return self._get_repr(repr)
