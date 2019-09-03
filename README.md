# TemperatureLogger
A simple python service to read temperatures from two TMP100 sensors via i2c,
log results to a CSV file and provide instant readings from an HTTP server.

Requires quick2wire for i2c communication. Uses Adafruit_LCDPlate.py to display temperatures on LCD (if present).

TODO:
- temperature_logger.py was the original, used for multiple TMP100's.
- tmp102_server.py is a WIP to use the TMP102 class.
- Need to consolidate both TMP100 and TMP102 into one class.
- Use a venv to load dependencies.
