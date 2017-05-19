#!/usr/bin/env python

from __future__ import print_function

import json
import signal
import subprocess
import sys
from datetime import datetime
from os import environ, utime
from threading import Timer
from time import time

from socketIO_client import BaseNamespace, SocketIO
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

NIGHTSCOUT_HOST = environ['NIGHTSCOUT_HOST']
API_SECRET = environ['API_SECRET']

MILLIS_PER_SECOND = 1000.

wait_count = 0


# from https://gist.github.com/walkermatt/2871026
def debounce(wait):
    """ Decorator that will postpone a functions
        execution until after wait seconds
        have elapsed since the last time it was invoked. """

    def decorator(fn):
        def debounced(*args, **kwargs):
            def call_it():
                fn(*args, **kwargs)

            try:
                debounced.t.cancel()
            except AttributeError:
                pass
            debounced.t = Timer(wait, call_it)
            debounced.t.start()

        return debounced

    return decorator


def throttle(wait):
    """ Decorator that will throttle a functions
        execution so that it occurs at most once every
        wait seconds. """

    def decorator(fn):
        def throttled(*args, **kwargs):
            def call_it():
                fn(*args, **kwargs)
                throttled.executed = time()

            try:
                throttled.t.cancel()
                if time() < throttled.executed + wait:
                    throttled.t = Timer(wait, call_it)
                    throttled.t.start()

            except AttributeError:
                call_it()
                pass

        return throttled

    return decorator



def timestamp_hours_ago(hours):
    return time() - hours * 60 * 60 * MILLIS_PER_SECOND


def write_glucose_json(entries):
    print("writing monitor/glucose.json")
    if entries:
        entry_str = json.dumps(entries)
        latest_time = entries[0]['date'] / MILLIS_PER_SECOND
        with open('monitor/glucose.json', 'w') as glucose_file:
            glucose_file.write(entry_str)
        utime('monitor/glucose.json', (latest_time, latest_time))
        print("Glucose data updated with timestamp {}".format(
            datetime.fromtimestamp(latest_time).isoformat()))


def write_treatment_json(filename, treatments):
    if treatments:
        treatment_str = json.dumps(treatments)
        latest_time = treatments[0]['mills'] / MILLIS_PER_SECOND
        with open(filename, 'w') as treatment_file:
            treatment_file.write(treatment_str)
        utime(filename, (latest_time, latest_time))
        print("Treatment ({}) data updated with timestamp {}".format(
            filename,
            datetime.fromtimestamp(latest_time).isoformat()
        ))


class MonitorEventHandler(FileSystemEventHandler):
    def __init__(self, io, treatment_map):
        self.io = io
        self.treatment_map = treatment_map
        super(MonitorEventHandler, self).__init__()

    def update_battery_status(self):
        """sudo ~/src/EdisonVoltage/voltage json batteryVoltage battery >
        monitor/edison-battery.json"""

    @debounce(10)
    def invoke_reconcile_treatments(self):
        print("invoke_reconcile_treatments")
        formatted_treatments = subprocess.check_output([
            "mm-format-ns-treatments",
            "monitor/pumphistory-zoned.json",
            "settings/model.json"])
        treatments_json = json.loads(formatted_treatments)
        for treatment in treatments_json:
            treatment_key = (treatment['eventType'], treatment['timestamp'])
            if treatment_key not in self.treatment_map:
                print(treatment_key, "being added.")
                for key, value in treatment.items():
                    if isinstance(value, str):
                        if value.isdigit():
                            treatment[key] = int(value)
                        else:
                            try:
                                treatment[key] = float(value)
                            except ValueError:
                                pass

                if treatment.get("duration") is not None:
                    treatment["duration"] = int(treatment["duration"])
                self.io.emit(
                    'dbAdd', {"collection": "treatments", "data": treatment})
                print(treatment_key, "added.")

    @throttle(15)
    def invoke_meal_json(self):
        print("invoke_meal_json")
        subprocess.call(["openaps", "report", "invoke", "monitor/meal.json"])

    @debounce(15)
    def invoke_settings_profile_json(self):
        print("invoke_settings_profile_json")
        subprocess.call(['openaps', 'report', 'invoke',
                         'settings/profile.json'])

    @debounce(10)
    def invoke_ns_status(self):
        print("invoke_ns_status")
        subprocess.call(
            ["openaps", "battery-status"])
        status = subprocess.check_output(["/usr/local/bin/ns-status",
                                          "monitor/clock-zoned.json",
                                          "monitor/iob.json",
                                          "enact/suggested.json",
                                          "enact/enacted.json",
                                          "monitor/battery.json",
                                          "monitor/reservoir.json",
                                          "monitor/status.json",
                                          "--uploader",
                                          "monitor/edison-battery.json"])
        if "iob" in status:
            self.io.emit('dbAdd', {
                "collection": "devicestatus",
                "data": json.loads(status)
            })

    def on_modified(self, event):
        if event.src_path == './.git':
            return
        if event.event_type == 'modified':
            if event.src_path in {"./settings/profile.json",
                                  "./monitor/carbhistory.json",
                                  "./monitor/clock-zoned.json",
                                  "./monitor/pumphistory-zoned.json",
                                  "./settings/basal_profile.json",
                                  "./monitor/glucose.json"}:
                print("queueing invoke_meal_json because {}".format(
                    event.src_path))
                self.invoke_meal_json()
            if event.src_path in {"./settings/bg_targets.json",
                                  "./preferences.json",
                                  "./settings/settings.json",
                                  "./settings/basal_profile.json",
                                  "./settings/carb_ratios.json",
                                  "./settings/temptargets.json",
                                  "./settings/model.json",
                                  "./settings/autotune.json",
                                  "./settings/insulin_sensitivities.json"}:
                print("queueing invoke_settings_profile_json because {}".format(
                    event.src_path))
                self.invoke_settings_profile_json()
            if event.src_path in {"./monitor/clock-zoned.json",
                                  "./monitor/iob.json",
                                  "./enact/suggested.json",
                                  "./enact/enacted.json",
                                  "./monitor/battery.json",
                                  "./monitor/reservoir.json",
                                  "./monitor/status.json"}:
                print("queueing invoke_ns_status because {}".format(
                    event.src_path))
                self.invoke_ns_status()
            if event.src_path == "./monitor/pumphistory-zoned.json":
                print("queueing invoke_reconcile_treatments")
                self.invoke_reconcile_treatments()


class WatchdogTimer(Exception):
    def __init__(self, timeout):
        self.timeout = timeout

    def __enter__(self):
        signal.signal(signal.SIGALRM, self.handler)
        signal.alarm(self.timeout)

    def __exit__(self, *args):
        signal.alarm(0)

    def handler(self, *args):
        print("alarm fired")
        raise self


class NightscoutNamespace(BaseNamespace):
    def __init__(self, io, path):
        print("__init__", io, path)
        self.io = io
        self.websocket_entries = []
        self.websocket_treatments = {}

        self.websocket_temp_targets = []
        self.websocket_meal_carbs = []

        self.pending_confirmations = {}

        self.observer = None
        super(NightscoutNamespace, self).__init__(io, path)

    def on_error(self, data):
        print("Error {}, restarting.".format(data))
        sys.stdout.flush()
        sys.exit()

    def on_close(self):
        print("Close, restarting.")
        sys.stdout.flush()
        sys.exit()

    def on_dataUpdate(self, data):
        try:
            global wait_count
            wait_count = 0
            print("Incoming ({}) received at {}.".format(
                data.keys(), datetime.now().isoformat()
            ))
            sys.stdout.flush()

            timestamp_1d = timestamp_hours_ago(24)
            if 'devicestatus' in data:
                self.handle_devicestatus(data)

            if 'sgvs' in data:
                self.handle_sgvs(data, timestamp_1d)

            if 'treatments' in data:
                self.handle_treatments(data)

        except Exception as e:
            print(e, e.message, e.args)
            import traceback
            traceback.print_exc()
            sys.exit()

    def handle_treatments(self, data):
        for treatment in data.get('treatments', ()):
            if treatment.get('action', None) and treatment['action'] \
                    == 'remove':
                print("removing treatment {}".format(treatment))
                removed_treatments = [
                    removed_treatment for
                    removed_treatment in
                    self.websocket_treatments.values() if
                    removed_treatment['_id'] ==
                    treatment['_id']]
                for removed_treatment in removed_treatments:
                    try:
                        del self.websocket_treatments[(
                            removed_treatment['eventType'],
                            treatment.get(
                                'timestamp',
                                treatment.get(
                                    'created_at',
                                    None
                                )
                            ))]
                    except KeyError:
                        pass
            else:
                self.websocket_treatments[(treatment['eventType'],
                                           treatment.get(
                                               'timestamp',
                                               treatment.get(
                                                   'created_at',
                                                   None
                                               )))] = treatment
        self.handle_temp_targets()
        self.handle_meal_carbs()

    def handle_sgvs(self, data, timestamp_1d):
        for entry in data.get('sgvs', ()):
            self.websocket_entries.append({
                'direction': entry.get('direction'),
                'date': entry.get('mills'),
                'dateString': datetime.fromtimestamp(
                    entry['mills'] / MILLIS_PER_SECOND,
                    tz=None).isoformat() + "-0500",
                'sgv': entry.get('mgdl'),
                'device': entry.get('device'),
                'rssi': entry.get('rssi'),
                'filtered': entry.get('filtered'),
                'unfiltered': entry.get('unfiltered'),
                'noise': entry.get('noise'),
                'type': 'sgv',
                'glucose': entry.get('mgdl')
            })
            # websocket_entries.append(entry)
            # print websocket_entries[-1]
        websocket_entries = [
            entry for entry in self.websocket_entries if
            entry.get('date') > timestamp_1d
        ]
        websocket_entries.sort(
            key=lambda k: k.get('date'),
            reverse=True)
        write_glucose_json(websocket_entries)

    def perform_bolus(self, amount):
        self.pending_confirmations = {}
        p = subprocess.Popen(
            ["openaps", "use", "pump", "bolus", "-"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
        print("Bolusing: %s" % ('{"units": %3.2f}' % amount))
        stdout, stderr = p.communicate('{"units": %3.2f}' % amount)
        print("Bolused: " + stdout + stderr)

    @debounce(1)
    def ack_bolus_request(self, token, amount):
        requester, confirmation = token
        self.pending_confirmations = {token: amount}
        device = "openaps://apstwo"
        status = {
            "device": device,
            "bolusack": {
                'units': amount,
                'target': device,
                'initiator': requester,
                'confirmation': confirmation
            }
        }
        print("Emitting:", status)
        self.io.emit('dbAdd', {
            "collection": "devicestatus",
            "data": status
        })

    def handle_devicestatus(self, data):
        for status in data.get('devicestatus', ()):
            if 'bolus' in status:
                print("Incoming bolus devicestatus: {}".format(status))
                my_id = "openaps://apstwo"
                bolus_amount = status['bolus']['units']
                target_device_id = status['bolus']['target']
                if target_device_id != my_id:
                    continue
                requesting_device_id = status['device']
                confirmation = int(status['bolus'].get('confirmation', 0))
                if self.pending_confirmations.get(
                        (requesting_device_id, confirmation)) == bolus_amount:
                    print("bolusing because of {}".format(status))
                    del self.pending_confirmations[(requesting_device_id,
                                                    confirmation)]
                    self.perform_bolus(bolus_amount)
                else:

                    self.ack_bolus_request((requesting_device_id,
                                            int(time())), bolus_amount)


    def handle_meal_carbs(self):
        new_websocket_meal_carbs = [
            treatment for treatment in self.websocket_treatments.values() if
            (treatment.get('carbs') or treatment.get('insulin')) and
            treatment.get('mills') >= timestamp_hours_ago(24)
        ]
        new_websocket_meal_carbs.sort(
            key=lambda t: t.get('mills'),
            reverse=True)
        if self.websocket_meal_carbs != new_websocket_meal_carbs:
            self.websocket_meal_carbs = new_websocket_meal_carbs
            write_treatment_json(
                'monitor/carbhistory.json',
                self.websocket_meal_carbs)

    def handle_temp_targets(self):
        new_websocket_temp_targets = [
            treatment for treatment in self.websocket_treatments.values() if
            'Target' in treatment.get('eventType', '') and
            treatment.get('mills') >= timestamp_hours_ago(6)
        ]
        new_websocket_temp_targets.sort(
            key=lambda t: t.get('mills'),
            reverse=True)
        if new_websocket_temp_targets != self.websocket_temp_targets:
            self.websocket_temp_targets = new_websocket_temp_targets
            write_treatment_json(
                'settings/temptargets.json',
                self.websocket_temp_targets)

    def on_connect(self, *args):
        self.io.emit('authorize', {
            'client': 'openaps-ws',
            'secret': API_SECRET,
            'history': 24,
            'from': timestamp_hours_ago(24),
            'status': False
        })
        path = '.'
        self.observer = Observer()
        self.observer.schedule(
            MonitorEventHandler(self.io, self.websocket_treatments),
            path,
            recursive=True)
        self.observer.start()
        super(NightscoutNamespace, self).on_connect()

    def on_disconnect(self):
        global wait_count
        wait_count += 8
        print(
            "Disconnected, aborting at {}."
                .format(datetime.now().isoformat()))
        sys.stdout.flush()
        if self.observer:
            self.observer.stop()
        sys.exit()


def main():
    with SocketIO(
            NIGHTSCOUT_HOST,
            Namespace=NightscoutNamespace,
            wait_for_connection=False) as socketIO:
        global wait_count
        while wait_count < 7:
            print("waiting at {}".format(datetime.now()))
            sys.stdout.flush()
            with WatchdogTimer(120):
                socketIO.wait(60)
            wait_count += 1
            sys.stdout.flush()

        print(
            "Waited too long for data.  Exiting at {}.".format(datetime.now()))


if __name__ == '__main__':
    main()
