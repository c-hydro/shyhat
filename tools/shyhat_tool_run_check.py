"""SHYHAT operational-chain availability and run monitor."""

import json
import logging
import netrc
import os
import smtplib
import time
from glob import glob
from argparse import ArgumentParser
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import pandas as pd
import pygsheets


ALG_NAME = "SHYHAT - OPERATIONAL CHAIN AVAILABILITY AND RUN MONITOR"
ALG_VERSION = "2.3.0"
ALG_RELEASE = "2026-08-20"


def main():
    settings_file, algorithm_time, skip_spreadsheet, skip_email = get_args()
    settings = read_file_json(settings_file)

    log_settings = settings["data"]["log"]
    os.makedirs(log_settings["folder"], exist_ok=True)
    set_logging(os.path.join(log_settings["folder"], log_settings["filename"]))

    logging.info(" ============================================================================ ")
    logging.info(" ==> %s (Version: %s Release_Date: %s)", ALG_NAME, ALG_VERSION, ALG_RELEASE)
    logging.info(" ==> START")
    start_time = time.time()

    time_run = datetime.strptime(algorithm_time, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    templates = settings["algorithm"]["template"]
    filled_template = fill_time_template(templates, time_run)

    models_check = check_models(settings, time_run, templates)
    runs_check = check_runs(settings, time_run, templates)

    outcome_settings = settings["data"]["dynamic"]["outcome"]
    check_outcome = outcome_settings["check"]
    report_folder = check_outcome["folder"].format(**filled_template)
    report_file = check_outcome["file_name"].format(**filled_template)
    os.makedirs(report_folder, exist_ok=True)
    write_report(
        report_folder,
        report_file,
        models_check,
        runs_check,
        settings["algorithm"]["general"]["operational_chain_name"],
        time_run,
    )

    if settings["algorithm"]["flags"]["public_check_spreadsheet"] and not skip_spreadsheet:
        titles = check_outcome.get(
            "titles", {"models": "FORECAST PRODUCTS", "runs": "OPERATIONAL PROCESSES"}
        )
        public_online_gdrive(
            check_outcome["url"],
            check_outcome["api_key"],
            models_check,
            runs_check,
            time_run,
            titles,
        )

    if settings["algorithm"]["flags"].get("send_email", False) and not skip_email:
        email_settings = outcome_settings.get("email")
        if not email_settings:
            raise ValueError("Email delivery is enabled but outcome.email is not configured")
        report_path = os.path.join(report_folder, report_file)
        with open(report_path, "r") as report_handle:
            report_text = report_handle.read()
        warning_count = count_warnings(models_check, runs_check)
        send_report_email(
            email_settings,
            report_text,
            settings["algorithm"]["general"]["operational_chain_name"],
            warning_count,
        )

    logging.info(" ==> TIME ELAPSED: %.1f seconds", time.time() - start_time)
    logging.info(" ==> END")
    logging.info(" ============================================================================ ")


def check_models(settings, time_run, templates):
    rows = []
    for model_name, model in settings["data"]["dynamic"]["models"].items():
        if not model["actions"]["check"]:
            continue

        logging.info("Checking product: %s", model["full_name"])
        file_template = os.path.join(model["folder"], model["file_name"])
        eta_value = build_eta(time_run, model.get("eta"))
        eta_text = eta_value.strftime("%Y-%m-%d %H:%M") if eta_value else "N/A"
        if model["type"] == "forecast":
            filled_template = fill_time_template(templates, time_run)
            file_now = file_template.format(**filled_template)
            status = check_forecast_availability(file_now, eta_value, model)
        elif model["type"] == "realtime":
            status = check_realtime_availability(file_template, templates, model)
        else:
            status = "WARNING! Unsupported product type: {}".format(model["type"])

        note = build_problem_note(status, resolve_log_file(model, templates, time_run))
        rows.append((model["full_name"], status, eta_text, note))

    return pd.DataFrame(
        rows, columns=["Product", "Status", "ETA (UTC)", "Details"]
    ).set_index("Product")


def check_runs(settings, time_run, templates):
    columns = ["Run status", "Time start", "Time end", "Expected end", "Status check", "Details"]
    rows = []

    for run_name, run in settings["data"]["dynamic"]["runs"].items():
        if not run["actions"]["check"]:
            continue

        logging.info("Checking process: %s", run["full_name"])
        if run.get("check_mode") == "recent_success":
            filled_template = fill_time_template(templates, time_run)
            end_lock_pattern = run["end_lock_pattern"].format(**filled_template)
            hours_delay = float(run["hours_delay"])
            run_status, time_start, time_end, run_check = check_recent_success(
                end_lock_pattern, hours_delay
            )
            note = build_problem_note(
                run_check, resolve_log_file(run, templates, time_run)
            )
            rows.append(
                (
                    run["full_name"],
                    run_status,
                    time_start,
                    time_end,
                    "Success within {} hours".format(hours_delay),
                    run_check,
                    note,
                )
            )
            continue

        run_reference = time_run - timedelta(hours=int(run.get("time_delay_h", 0)))
        filled_template = fill_time_template(templates, run_reference)
        filled_template["run_name"] = run_name
        start_lock = run["start_lock_file"].format(**filled_template)
        end_lock = run["end_lock_file"].format(**filled_template)

        run_status, time_start, time_end, time_end_value, run_has_errors = check_run_state(
            start_lock, end_lock
        )
        eta_value = build_eta(time_run, run.get("eta"))
        expected_end = eta_value.strftime("%Y-%m-%d %H:%M") if eta_value else "UNKNOWN"
        run_check = check_run_condition(time_end_value, eta_value, run_has_errors)
        problem_status = run_check
        if run_status == "Unknown condition":
            problem_status = "WARNING! " + run_status
        note = build_problem_note(problem_status, resolve_log_file(run, templates, run_reference))

        rows.append(
            (
                run["full_name"],
                run_status,
                time_start,
                time_end,
                expected_end,
                run_check,
                note,
            )
        )

    return pd.DataFrame(rows, columns=["Process name"] + columns).set_index("Process name")


def check_recent_success(end_lock_pattern, hours_delay):
    if hours_delay <= 0:
        return "Invalid delay", "", "", "WARNING"

    end_locks = [path for path in glob(end_lock_pattern) if os.path.isfile(path)]
    if not end_locks:
        return "Has not run", "", "", "WARNING"

    latest_end = max(end_locks, key=os.path.getmtime)
    end_time = datetime.fromtimestamp(os.path.getmtime(latest_end), tz=timezone.utc)
    start_path = latest_end.replace("_END.txt", "_START.txt")
    if os.path.isfile(start_path):
        start_time = datetime.fromtimestamp(os.path.getmtime(start_path), tz=timezone.utc)
        start_text = start_time.strftime("%Y-%m-%d %H:%M")
    else:
        start_text = ""

    end_text = end_time.strftime("%Y-%m-%d %H:%M")
    if start_text and start_time > end_time:
        return "Is running", start_text, "", "OK"

    end_status = read_lock_status(latest_end)
    if end_status == "COMPLETED_WITH_ERRORS":
        return "Completed with errors", start_text, end_text, "WARNING"
    if end_status != "COMPLETED":
        return "Unknown condition", start_text, end_text, "WARNING"

    age_hours = (datetime.now(timezone.utc) - end_time).total_seconds() / 3600
    if age_hours > hours_delay:
        return "Last success too old", start_text, end_text, "WARNING"
    return "Has run", start_text, end_text, "OK"


def check_realtime_availability(file_template, templates, model):
    """Check recency and optional completeness of a realtime product.

    The check is split into two independent conditions:

    1. Recency: find the latest available file within ``search_period_hours``.
    2. Completeness: starting from that latest available file, optionally require
       a complete history of ``availability_length_hours`` sampled every
       ``availability_step_minutes``.

    Expected publication latency is therefore accepted. If the latest map is
    four hours old and ``search_period_hours`` is 6, the recency check passes.
    The completeness window is then evaluated backwards from that latest
    available map, not backwards from the current time.

    Legacy settings ``availability_period_hours`` and ``availability_step_hours``
    are still accepted as fallbacks.
    """
    try:
        search_period_hours = float(
            model.get("search_period_hours", model.get("hours_delay", 24))
        )
        availability_length_hours = float(
            model.get(
                "availability_length_hours",
                model.get("availability_period_hours", 0),
            )
        )

        if "availability_step_minutes" in model:
            availability_step_minutes = int(model["availability_step_minutes"])
        else:
            availability_step_minutes = int(
                float(model.get("availability_step_hours", 1)) * 60
            )
    except (TypeError, ValueError):
        return "WARNING! Invalid realtime availability settings"

    if (
        search_period_hours < 0
        or availability_length_hours < 0
        or availability_step_minutes <= 0
    ):
        return "WARNING! Invalid realtime availability settings"

    availability_length_minutes = availability_length_hours * 60
    expected_steps_float = availability_length_minutes / availability_step_minutes

    if availability_length_hours > 0 and abs(
        expected_steps_float - round(expected_steps_float)
    ) > 1e-9:
        return (
            "WARNING! availability_length_hours must be an exact multiple of "
            "availability_step_minutes"
        )

    # Align current UTC time to the product temporal grid. This also supports
    # sub-hourly products such as 30-minute IMERG data.
    step_seconds = availability_step_minutes * 60
    time_now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    aligned_timestamp = int(time_now.timestamp())
    aligned_timestamp -= aligned_timestamp % step_seconds
    time_now = datetime.fromtimestamp(aligned_timestamp, tz=timezone.utc)

    # Search backwards from now only to establish product recency.
    search_period_minutes = int(round(search_period_hours * 60))
    last_available = None
    for offset_minutes in range(
        0,
        search_period_minutes + 1,
        availability_step_minutes,
    ):
        time_check = time_now - timedelta(minutes=offset_minutes)
        path = file_template.format(**fill_time_template(templates, time_check))
        if os.path.isfile(path):
            last_available = time_check
            break

    if last_available is None:
        return "WARNING! No data found in the last {} hours".format(
            search_period_hours
        )

    last_text = last_available.strftime("%Y-%m-%d %H:%M")

    # If no completeness length is configured, recency alone is sufficient.
    if availability_length_hours == 0:
        return "OK! Last available data: {}".format(last_text)

    # Check exactly N expected product steps backwards from the latest available
    # timestamp. Example: 24 h at 30 min = 48 expected files, including the
    # latest timestamp and ending 23 h 30 min before it.
    expected_steps = int(round(expected_steps_float))
    missing = []
    for step_index in range(expected_steps):
        time_check = last_available - timedelta(
            minutes=step_index * availability_step_minutes
        )
        path = file_template.format(**fill_time_template(templates, time_check))
        if not os.path.isfile(path):
            missing.append(time_check.strftime("%Y-%m-%d %H:%M"))

    if missing:
        return (
            "WARNING! Last available data: {}. Incomplete previous {} hours "
            "at {}-minute steps; expected {} files, missing {}: {}"
        ).format(
            last_text,
            availability_length_hours,
            availability_step_minutes,
            expected_steps,
            len(missing),
            ", ".join(missing),
        )

    return (
        "OK! Last available data: {}. Complete previous {} hours at "
        "{}-minute steps ({} files)"
    ).format(
        last_text,
        availability_length_hours,
        availability_step_minutes,
        expected_steps,
    )

def check_forecast_availability(file_now, eta_value, model=None):
    if os.path.isfile(file_now):
        variables_status = check_netcdf_variable_count(file_now, model or {})
        if variables_status:
            return variables_status
        modified = datetime.fromtimestamp(os.path.getmtime(file_now), tz=timezone.utc)
        return "OK! Product available at " + modified.strftime("%Y-%m-%d %H:%M")
    if eta_value is not None and eta_value > datetime.now(timezone.utc):
        return "OK! Product not available yet"
    return "WARNING! Product is not available: " + file_now


def check_netcdf_variable_count(file_path, model):
    """Validate an optional minimum number of NetCDF data variables.

    Dimension coordinates and CF grid-mapping variables are excluded.  Returning
    None means either that the check is not configured or that it passed.
    """
    expected_value = model.get("number_of_vars")
    if expected_value is None:
        return None
    try:
        expected = int(expected_value)
    except (TypeError, ValueError):
        return "WARNING! Invalid number_of_vars setting: {}".format(expected_value)
    if expected < 1:
        return "WARNING! Invalid number_of_vars setting: {}".format(expected_value)

    try:
        from netCDF4 import Dataset

        with Dataset(file_path, "r") as dataset:
            data_variables = []
            for variable_name, variable in dataset.variables.items():
                if variable_name in dataset.dimensions:
                    continue
                if getattr(variable, "grid_mapping_name", None):
                    continue
                data_variables.append(variable_name)
    except Exception as exc:
        return "WARNING! Product incomplete: unable to inspect NetCDF variables in {} ({})".format(
            file_path, exc
        )

    found = len(data_variables)
    if found < expected:
        return (
            "WARNING! Product incomplete: found {} data variable(s), expected at least {}: {}"
        ).format(found, expected, file_path)
    return None


def check_run_state(start_lock_file, end_lock_file):
    start_exists = os.path.isfile(start_lock_file)
    end_exists = os.path.isfile(end_lock_file)
    if start_exists and end_exists:
        time_start = datetime.fromtimestamp(os.path.getmtime(start_lock_file), tz=timezone.utc)
        time_end_value = datetime.fromtimestamp(os.path.getmtime(end_lock_file), tz=timezone.utc)
        end_status = read_lock_status(end_lock_file)
        has_errors = end_status == "COMPLETED_WITH_ERRORS"
        return (
            "Completed with errors" if has_errors else "Has run",
            time_start.strftime("%Y-%m-%d %H:%M"),
            time_end_value.strftime("%Y-%m-%d %H:%M"),
            time_end_value,
            has_errors,
        )
    if start_exists and not end_exists:
        time_start = datetime.fromtimestamp(os.path.getmtime(start_lock_file), tz=timezone.utc)
        return "Is running", time_start.strftime("%Y-%m-%d %H:%M"), "", None, False
    if not start_exists and not end_exists:
        return "Has not run", "", "", None, False
    return "Unknown condition", "", "", None, True


def read_lock_status(lock_file):
    with open(lock_file, "r", errors="replace") as file_handle:
        for line in file_handle:
            if "Status:" in line:
                return line.split("Status:", 1)[1].strip()
    return "UNKNOWN"


def check_run_condition(time_end_value, eta_value, has_errors=False):
    if has_errors:
        return "WARNING"
    if time_end_value is not None:
        return "OK"
    if eta_value is None:
        return "UNKNOWN"
    if eta_value < datetime.now(timezone.utc):
        return "WARNING"
    return "OK, wait for scheduled time"


def resolve_log_file(item, templates, reference_time):
    log_file = item.get("log_file")
    if not log_file:
        return None
    return log_file.format(**fill_time_template(templates, reference_time))


def build_problem_note(status, log_file):
    if status.startswith("OK"):
        return ""
    if not log_file:
        return "No log configured"
    if not os.path.isfile(log_file):
        return "LOG NOT FOUND at path: {}".format(log_file)

    last_error = None
    with open(log_file, "r", errors="replace") as log_handle:
        for line in log_handle:
            if "ERROR" in line:
                last_error = line.strip()
    if last_error:
        return last_error
    return "No error found in log: {}".format(log_file)


def build_eta(time_run, eta):
    if not eta:
        return None
    hours, minutes = (int(value) for value in eta.split(":"))
    return time_run.replace(hour=hours, minute=minutes, second=0, microsecond=0)


def write_report(out_folder, out_name, models_check, runs_check, chain_name, time_run):
    report_path = os.path.join(out_folder, out_name)
    with open(report_path, "w") as report_file:
        report_file.write("============================================================\n")
        report_file.write(
            "{} RECAP - Check time: {}\n".format(
                chain_name, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
            )
        )
        report_file.write("Reference time: {}\n".format(time_run.strftime("%Y-%m-%d %H:%M")))
        report_file.write("============================================================\n\n")
        report_file.write("Forecast products\n")
        for name, row in models_check.iterrows():
            report_file.write("{}: {}".format(name, row["Status"]))
            if row["Details"]:
                report_file.write(" - Note: {}".format(row["Details"]))
            report_file.write("\n")
        report_file.write("\nOperational processes\n")
        for name, row in runs_check.iterrows():
            report_file.write("{}: {} - {}".format(name, row["Run status"], row["Status check"]))
            if row["Details"]:
                report_file.write(" - Note: {}".format(row["Details"]))
            report_file.write("\n")


def count_warnings(models_check, runs_check):
    model_warnings = sum(
        str(status).startswith("WARNING") for status in models_check["Status"].values
    )
    run_warnings = sum(
        str(status).startswith("WARNING") for status in runs_check["Status check"].values
    )
    return model_warnings + run_warnings


def send_report_email(email_settings, report_text, chain_name, warning_count):
    smtp_server = email_settings["smtp_server"]
    smtp_port = int(email_settings.get("smtp_port", 587))
    recipients_value = email_settings.get("recipient", [])
    if isinstance(recipients_value, str):
        recipients = [item.strip() for item in recipients_value.split(",") if item.strip()]
    else:
        recipients = [str(item).strip() for item in recipients_value if str(item).strip()]
    if not recipients:
        raise ValueError("Email delivery is enabled but no recipients are configured")

    user = email_settings.get("user", "")
    password = email_settings.get("pwd", "")
    if not user or not password:
        credentials = netrc.netrc().authenticators(smtp_server)
        if credentials is None:
            raise ValueError(
                "No SMTP credentials found for {} in settings or .netrc".format(smtp_server)
            )
        user, _, password = credentials

    sender = email_settings.get("sender") or user
    subject_template = email_settings.get(
        "subject", "{operational_chain_name}: operational chain recap"
    )
    subject = subject_template.format(operational_chain_name=chain_name)
    if warning_count:
        subject += " -- WARNING!"

    message = EmailMessage()
    message["Subject"] = subject
    message["To"] = ", ".join(recipients)
    message["From"] = sender
    message.set_content(report_text + email_settings.get("other_infos", ""))

    session = smtplib.SMTP(smtp_server, smtp_port, timeout=30)
    try:
        session.ehlo()
        if email_settings.get("starttls", True):
            session.starttls()
            session.ehlo()
        session.login(user, password)
        session.send_message(message)
    finally:
        session.quit()


def public_online_gdrive(url, api_key, models_check, runs_check, time_run, titles):
    gc = pygsheets.authorize(service_file=api_key)
    worksheet = gc.open_by_url(url)[0]
    worksheet.clear()

    worksheet.cell("A1").set_text_format("bold", True).value = "RUN TIME (UTC)"
    worksheet.cell("B1").value = time_run.strftime("%Y-%m-%d %H:%M")
    worksheet.cell("C1").set_text_format("bold", True).value = "CHECK TIME (UTC)"
    worksheet.cell("D1").value = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")

    model_title_row = 3
    model_header_row = 4
    model_first_row = 5
    worksheet.cell("A{}".format(model_title_row)).set_text_format("bold", True).value = titles["models"]
    worksheet.cell("A{}".format(model_header_row)).set_text_format("bold", True).value = "Product"
    worksheet.cell("B{}".format(model_header_row)).set_text_format("bold", True).value = "Status"
    worksheet.cell("C{}".format(model_header_row)).set_text_format("bold", True).value = "ETA (UTC)"
    worksheet.cell("D{}".format(model_header_row)).set_text_format("bold", True).value = "Details"
    worksheet.set_dataframe(models_check, (model_first_row, 1), copy_index=True, copy_head=False)
    model_last_row = model_first_row + len(models_check) - 1
    add_status_formatting(worksheet, "B", model_first_row, model_last_row)

    run_title_row = model_last_row + 3
    run_header_row = run_title_row + 1
    run_first_row = run_header_row + 1
    worksheet.cell("A{}".format(run_title_row)).set_text_format("bold", True).value = titles["runs"]
    headers = ["Process name", "Run status", "Run start (UTC)", "Run end (UTC)", "Expected end (UTC)", "Status check", "Details"]
    for column, header in enumerate(headers, start=1):
        worksheet.cell((run_header_row, column)).set_text_format("bold", True).value = header
    worksheet.set_dataframe(runs_check, (run_first_row, 1), copy_index=True, copy_head=False)
    run_last_row = run_first_row + len(runs_check) - 1
    add_status_formatting(worksheet, "F", run_first_row, run_last_row)


def add_status_formatting(worksheet, column, first_row, last_row):
    if last_row < first_row:
        return
    worksheet.add_conditional_formatting(
        "{}{}".format(column, first_row),
        "{}{}".format(column, last_row),
        "TEXT_CONTAINS",
        format={"backgroundColor": {"green": 0.75, "red": 0.1, "blue": 0.1}},
        condition_values=["OK"],
    )
    worksheet.add_conditional_formatting(
        "{}{}".format(column, first_row),
        "{}{}".format(column, last_row),
        "TEXT_CONTAINS",
        format={"backgroundColor": {"green": 0.7, "red": 0.7, "blue": 0.7}},
        condition_values=["UNKNOWN"],
    )
    worksheet.add_conditional_formatting(
        "{}{}".format(column, first_row),
        "{}{}".format(column, last_row),
        "TEXT_CONTAINS",
        format={"backgroundColor": {"green": 0.1, "red": 0.95, "blue": 0.05}},
        condition_values=["WARNING"],
    )


def fill_time_template(templates, time_value):
    return {key: time_value.strftime(value) for key, value in templates.items()}


def read_file_json(file_name):
    with open(file_name, "r") as file_handle:
        content = file_handle.read()
    for env_key, env_value in os.environ.items():
        content = content.replace("$" + env_key, env_value.strip("'\\\""))
    return json.loads(content)


def get_args():
    parser = ArgumentParser()
    parser.add_argument("-settings_file", dest="settings_file", default="configuration.json")
    parser.add_argument("-time", dest="algorithm_time", required=True)
    parser.add_argument(
        "-skip_spreadsheet",
        action="store_true",
        help="Run all checks and reports without updating Google Sheets",
    )
    parser.add_argument(
        "-skip_email",
        action="store_true",
        help="Run all checks and reports without sending email",
    )
    values = parser.parse_args()
    return values.settings_file, values.algorithm_time, values.skip_spreadsheet, values.skip_email


def set_logging(logger_file):
    logger_format = (
        "%(asctime)s %(name)-12s %(levelname)-8s "
        "%(filename)s:[%(lineno)-6s - %(funcName)20s()] %(message)s"
    )
    logging.root.handlers.clear()
    logging.root.setLevel(logging.INFO)
    file_handler = logging.FileHandler(logger_file, "w")
    stream_handler = logging.StreamHandler()
    formatter = logging.Formatter(logger_format)
    file_handler.setFormatter(formatter)
    stream_handler.setFormatter(formatter)
    logging.root.addHandler(file_handler)
    logging.root.addHandler(stream_handler)


if __name__ == "__main__":
    main()
