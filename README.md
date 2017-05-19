# sioaps
A SocketIO-based OpenAPS data source

This replaces the nightscout-loop cron line in OpenAPS.  Don't use it along with ns-upload or nightscout BG fetches.

I place the Python script in `~/myopenaps/monitor` and use the following cron line to run it:
```
* * * * * cd /root/myopenaps && ps aux | grep -v grep | grep -q 'python ./monitor/siobg.py' || ./monitor/siobg.py 2>&1 | tee -a /var/log/openaps/siobg.log
```
