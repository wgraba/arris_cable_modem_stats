"""
    Pull stats from Arris Cable modem's web interface
    Send stats to InfluxDB

    https://github.com/andrewfraley/arris_cable_modem_stats
"""
# pylint: disable=line-too-long

import os
import sys
import time
import base64
import logging
import argparse
import configparser
from datetime import datetime
import influxdb_client
import urllib3
import requests


# To add a new modem, add the model below
# Create a new file src/arris_stats_themodel.py and a parse_html_themodel.py function
# Use a debugger and set a break point just after the html = get_html(config, credential) line
# Set another break point just after the  stats = parse_html_function(html) line
# Save the raw html to tests/mockups/themodel.html
# Save the stats dict as json to tests/mockups/themodel.json
# The unittests will automatically pickup the new model, function, and mockups.  Ensure the tests pass with:
# bash tests/run_tests.sh
modems_supported = ["sb8200", "sb6183"]

# The modem is pretty finicky about the headers
HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Cache-Control": "max-age=",
    "Connection": "keep-alive",
    "DNT": "1",
    "Upgrade-Insecure-Requests": "1",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:82.0) Gecko/20100101 Firefox/82.0",
}


def main():
    """MAIN"""

    args = get_args()
    init_logger(args.debug)

    config_path = args.config
    config = get_config(config_path)

    # Re-init the logger if we set arris_stats_debug in ENV or config.ini
    if config["arris_stats_debug"]:
        init_logger(True)

    sleep_interval = int(config["sleep_interval"])
    destination = config["destination"]

    # Disable the SSL warnings if we're not verifying SSL
    if not config["modem_verify_ssl"]:
        urllib3.disable_warnings()

    # SB8200 requires authentication on Comcast now
    credential = None

    first = True
    while True:
        if not first:
            logging.info("Sleeping for %s seconds", sleep_interval)
            sys.stdout.flush()
            time.sleep(sleep_interval)
        first = False

        if config["modem_auth_required"]:
            while not credential:
                credential = get_credential(config)
                if not credential and config["exit_on_auth_error"]:
                    error_exit(
                        "Unable to authenticate with modem.  Exiting since exit_on_auth_error is True",
                        config,
                    )
                if not credential:
                    logging.info(
                        "Unable to obtain valid login session, sleeping for: %ss",
                        sleep_interval,
                    )
                    time.sleep(sleep_interval)

        # Get the HTML from the modem
        html = get_html(config, credential)
        if not html:
            if config["exit_on_html_error"]:
                error_exit(
                    "No HTML obtained from modem.  Exiting since exit_on_html_error is True",
                    config,
                )
            logging.error("No HTML to parse, giving up until next interval")
            if config["clear_auth_token_on_html_error"]:
                logging.info(
                    "clear_auth_token_on_html_error is true, clearing credential token"
                )
                credential = None
            continue

        # Get the function reference from the config dict
        parse_html_function = config["parse_html_function"]
        stats = parse_html_function(html)

        if not stats or (not stats["upstream"] and not stats["downstream"]):
            logging.error("Failed to get any stats, giving up until next interval")
            continue

        # Where should we send the results?
        if destination == "influxdb":
            send_to_influx(stats, config)
        elif destination == "timestream":
            send_to_aws_time_stream(stats, config)
        else:
            error_exit(
                "Destination %s not supported!  Aborting." % destination, sleep=False
            )


def get_args():
    """Get argparser args"""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        metavar="config_file_path",
        help="Path to config file",
        required=True,
    )
    parser.add_argument(
        "--debug",
        help="Enable debug logging",
        action="store_true",
        required=False,
        default=False,
    )
    args = parser.parse_args()
    return args


def get_default_config():
    return {
        # Main
        "arris_stats_debug": False,
        "destination": "influxdb",
        "sleep_interval": 300,
        "modem_url": "https://192.168.100.1/cmconnectionstatus.html",
        "modem_verify_ssl": False,
        "modem_auth_required": False,
        "modem_username": "admin",
        "modem_password": None,
        "modem_model": "sb8200",
        "exit_on_auth_error": True,
        "exit_on_html_error": True,
        "clear_auth_token_on_html_error": True,
        "sleep_before_exit": True,
        # Influx
        "influx_host": "localhost",
        "influx_port": 8086,
        "influx_database": "cable_modem_stats",
        "influx_org": "myorg",
        "influx_username": None,
        "influx_password": None,
        "influx_token": "mytoken",
        "influx_use_ssl": False,
        "influx_verify_ssl": True,
        # AWS Timestream
        "timestream_aws_access_key_id": None,
        "timestream_aws_secret_access_key": None,
        "timestream_database": "cable_modem_stats",
        "timestream_table": "cable_modem_stats",
        "timestream_aws_region": "us-east-1",
    }


def get_config(config_path=None):
    """Grab config from the ini config file,
    then grab the same variables from ENV to override
    """

    default_config = get_default_config()
    config = default_config.copy()

    # Get config from config.ini first
    if config_path:

        # Some hacky action to get the config without using section headings in the file
        # https://stackoverflow.com/a/10746467/866057
        parser = configparser.RawConfigParser()
        section = "MAIN"
        with open(config_path) as f:
            file_content = "[%s]\n" % section + f.read()
        parser.read_string(file_content)

        for param in default_config:
            config[param] = parser[section].get(param, default_config[param])

    # Get it from ENV now and override anything we find
    for param in config:
        if os.environ.get(param):
            config[param] = os.environ.get(param)

    # Special handling depending ontype
    for param in config:

        # If the default value is a boolean, but we have a string, convert it
        if isinstance(default_config[param], bool) and isinstance(config[param], str):
            config[param] = str_to_bool(string=config[param], name=param)

        # If the default value is an int, but we have a string, convert it
        if isinstance(default_config[param], int) and isinstance(config[param], str):
            config[param] = int(config[param])

        # Finally any 'None' string should just be None
        if default_config[param] is None and config[param] == "None":
            config[param] = None

        # Ensure model model is supported
        if config["modem_model"] not in modems_supported:
            raise RuntimeError("Model model %s not supported!" % config["modem_model"])

    # This gets the correct function to use to parse the modem's html based on model
    # If you're adding new modems and get an error about no module, create src/arris_stats_yourmodel.py
    module = __import__("arris_stats_" + config["modem_model"])
    config["parse_html_function"] = getattr(
        module, "parse_html_" + config["modem_model"]
    )

    return config


def get_credential(config):
    """Get the cookie credential by sending the
    username and password pair for basic auth. They
    also want the pair as a base64 encoded get req param
    """
    logging.info("Obtaining login session from modem")

    url = config["modem_url"]
    username = config["modem_username"]
    password = config["modem_password"]
    verify_ssl = config["modem_verify_ssl"]

    # We have to send a request with the username and password
    # encoded as a url param.  Look at the Javascript from the
    # login page for more info on the following.
    token = username + ":" + password
    auth_hash = base64.b64encode(token.encode("ascii"))
    auth_url = url + "?" + auth_hash.decode()
    logging.debug("auth_url: %s", auth_url)

    # This is going to respond with our "credential", which is a hash that we
    # have to send as a cookie with subsequent requests
    try:
        resp = requests.get(
            auth_url, headers=HEADERS, auth=(username, password), verify=verify_ssl
        )

        if resp.status_code != 200:
            logging.error("Error authenticating with %s", url)
            logging.error("Status code: %s", resp.status_code)
            logging.error("Reason: %s", resp.reason)
            return None

        credential = resp.text
        resp.close()
    except Exception as exception:
        logging.error(exception)
        logging.error("Error authenticating with %s", url)
        return None

    if "Password:" in credential:
        logging.error(
            "Authentication error, received login page.  Check username / password.  SB8200 has some kind of bug that can cause this after too many authentications, the only known fix is to reboot the modem."
        )
        return None

    return credential


def get_html(config, credential):
    """Get the status page from the modem
    return the raw html
    """
    url = config["modem_url"]
    verify_ssl = config["modem_verify_ssl"]

    if config["modem_auth_required"]:
        cookies = {"credential": credential}
    else:
        cookies = None

    logging.info("Retreiving stats from %s", url)

    try:
        resp = requests.get(url, headers=HEADERS, cookies=cookies, verify=verify_ssl)
        if resp.status_code != 200:
            logging.error("Error retreiving html from %s", url)
            logging.error("Status code: %s", resp.status_code)
            logging.error("Reason: %s", resp.reason)
            return None
        status_html = resp.content.decode("utf-8")
        resp.close()
    except Exception as exception:
        logging.error(exception)
        logging.error("Error retreiving html from %s", url)
        return None

    if "Password:" in status_html:
        logging.error(
            "Authentication error, received login page.  Check username / password.  SB8200 has some kind of bug that can cause this after too many authentications, the only known fix is to reboot the modem."
        )
        if not config["modem_auth_required"]:
            logging.warning(
                "You have modem_auth_required to False, but a login page was detected!"
            )
        return None

    return status_html


def send_to_influx(stats, config):
    """Send the stats to InfluxDB"""
    logging.info(
        "Sending stats to InfluxDB (%s:%s)",
        config["influx_host"],
        config["influx_port"],
    )

    from influxdb_client import InfluxDBClient, Point
    from influxdb_client.client.write_api import SYNCHRONOUS
    from influxdb_client.rest import ApiException

    series = []
    current_time = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    for stats_down in stats["downstream"]:
        record = {
            "measurement": "downstream_statistics",
            "time": current_time,
            "fields": {},
            "tags": {"channel_id": int(stats_down["channel_id"])},
        }
        for field in stats_down:
            if field == "channel_id":
                continue
            if "." in stats_down[field]:
                record["fields"][field] = float(stats_down[field])
            else:
                record["fields"][field] = int(stats_down[field])

        series.append(record)

    for stats_up in stats["upstream"]:
        record = {
            "measurement": "upstream_statistics",
            "time": current_time,
            "fields": {},
            "tags": {"channel_id": int(stats_up["channel_id"])},
        }
        for field in stats_up:
            if field == "channel_id":
                continue
            if "." in stats_up[field]:
                record["fields"][field] = float(stats_up[field])
            else:
                record["fields"][field] = int(stats_up[field])

        series.append(record)

    with InfluxDBClient(
        url=f"http://{config['influx_host']}:{config['influx_port']}",
        org=config["influx_org"],
        token=config["influx_token"],
    ) as _client:

        with _client.write_api(write_options=SYNCHRONOUS) as _write_client:
            # _write_client.write(bucket=config["influx_database"], record=series)
            try:
                _write_client.write(bucket=config["influx_database"], record=series)

            except ApiException as exception:
                # If DB doesn't exist, try to create it
                if exception.status == 404:
                    logging.warning(
                        "Database %s Does Not Exist.  Attempting to create database",
                        config["influx_database"],
                    )
                    _bucket_client = _client.buckets_api()
                    _bucket_client.create_bucket(
                        bucket_name=config["influx_database"], org=config["influx_org"]
                    )
                    _write_client.write(bucket=config["influx_database"], record=series)

                else:
                    logging.error(exception)
                    logging.error("Failed To Write To InfluxDB")
                    return

            logging.info("Successfully wrote data to InfluxDB")
            logging.debug("Influx series sent to db:")
            logging.debug(series)


def send_to_aws_time_stream(stats, config):
    """Send the stats to AWS Timestream"""
    logging.info(
        "Sending stats to Timestream (database=%s)", config["timestream_database"]
    )

    import boto3
    from botocore.config import Config

    region_config = Config(region_name=config["timestream_aws_region"])

    ts_client = boto3.client(
        "timestream-write",
        aws_access_key_id=config["timestream_aws_access_key_id"],
        aws_secret_access_key=config["timestream_aws_secret_access_key"],
        config=region_config,
    )

    try:
        # Attempt to validate connection to database and table
        # Error out and return if not able to access / connection isn't valid
        database = ts_client.describe_database(
            DatabaseName=config["timestream_database"]
        )
        logging.debug("Database details = %s" % database)

        table = ts_client.describe_table(
            DatabaseName=config["timestream_database"],
            TableName=config["timestream_table"],
        )
        logging.debug("Table details = %s" % table)
    except Exception as err:
        logging.error(err)
        return

    current_time = time.time_ns()
    logging.debug("Converting to timestream - %s" % stats)

    downstream_common_attributes = {
        "Dimensions": [{"Name": "measurement", "Value": "downstream_statistics"}],
        "Time": str(current_time),
        "TimeUnit": "NANOSECONDS",
    }
    downstream_records = []
    for stats_down in stats["downstream"]:
        for key in stats_down:
            if key == "channel_id":
                continue

            downstream_records.append(
                {
                    "Dimensions": [
                        {"Name": "channel_id", "Value": str(stats_down["channel_id"])},
                        {"Name": "group", "Value": "downstream_statistics"},
                    ],
                    "MeasureName": key,
                    "MeasureValue": str(stats_down[key]),
                    "MeasureValueType": "DOUBLE"
                    if isinstance(stats_down[key], float)
                    else "BIGINT",
                }
            )

    try:
        logging.debug("Writing common attributes: %s" % downstream_common_attributes)
        logging.debug("Writing records: %s" % downstream_records)
        result = ts_client.write_records(
            DatabaseName=config["timestream_database"],
            TableName=config["timestream_table"],
            Records=downstream_records,
            CommonAttributes=downstream_common_attributes,
        )
        logging.info("Timestream response = %s" % result)
        logging.info("Wrote %s records to TimeStream" % len(downstream_records))
    except (ts_client.exceptions.RejectedRecordsException, Exception) as err:
        logging.error(err)

    upstream_common_attributes = {
        "Dimensions": [{"Name": "measurement", "Value": "upstream_statistics"}],
        "Time": str(current_time),
        "TimeUnit": "NANOSECONDS",
    }
    upstream_records = []
    for stats_up in stats["upstream"]:
        for key in stats_up:
            if key == "channel_id":
                continue

            upstream_records.append(
                {
                    "Dimensions": [
                        {"Name": "channel_id", "Value": str(stats_up["channel_id"])},
                        {"Name": "group", "Value": "upstream_statistics"},
                    ],
                    "MeasureName": key,
                    "MeasureValue": str(stats_up[key]),
                    "MeasureValueType": "DOUBLE"
                    if isinstance(stats_up[key], float)
                    else "BIGINT",
                }
            )

    try:
        logging.debug("Writing common attributes: %s" % upstream_common_attributes)
        logging.debug("Writing records: %s" % upstream_records)
        result = ts_client.write_records(
            DatabaseName=config["timestream_database"],
            TableName=config["timestream_table"],
            Records=upstream_records,
            CommonAttributes=upstream_common_attributes,
        )
        logging.info("Timestream response = %s" % result)
        logging.info("Wrote %s records to TimeStream" % len(upstream_records))
    except (ts_client.exceptions.RejectedRecordsException, Exception) as err:
        logging.error(err)
        return

    logging.info("Successfully wrote data to Timestream")


def error_exit(message, config=None, sleep=True):
    """Log error, sleep if needed, then exit 1"""
    logging.error(message)
    if sleep and config and config["sleep_before_exit"]:
        logging.info(
            "Sleeping for %s seconds before exiting since sleep_before_exit is True",
            config["sleep_interval"],
        )
        time.sleep(config["sleep_interval"])
    sys.exit(1)


def write_html(html):
    """write html to file"""
    with open("/tmp/html", "wb") as text_file:
        text_file.write(html)


def read_html():
    """read html from file"""
    with open("/tmp/html", "rb") as text_file:
        html = text_file.read()
    return html


def str_to_bool(string, name):
    """Return True is string ~= 'true'"""
    if string.lower() == "true":
        return True
    if string.lower() == "false":
        return False

    raise ValueError(
        'Config parameter % s should be boolean "true" or "false", but value is neither of those.'
        % name
    )


def init_logger(debug=False):
    """Start the python logger"""
    log_format = "%(asctime)s %(levelname)-8s %(message)s"

    if debug:
        level = logging.DEBUG
    else:
        level = logging.INFO

    # https://stackoverflow.com/a/61516733/866057
    try:
        root_logger = logging.getLogger()
        root_logger.setLevel(level)
        root_handler = root_logger.handlers[0]
        root_handler.setFormatter(logging.Formatter(log_format))
    except IndexError:
        logging.basicConfig(level=level, format=log_format)


if __name__ == "__main__":
    main()
