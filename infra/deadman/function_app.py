import json
import logging
import os
from datetime import datetime, timezone

import azure.functions as func
from azure.identity import DefaultAzureCredential
from azure.mgmt.cognitiveservices import CognitiveServicesManagementClient
from azure.mgmt.resource import ResourceManagementClient

from deadman_core import evaluate

app = func.FunctionApp()


@app.timer_trigger(schedule="0 */5 * * * *", arg_name="timer", run_on_startup=False, use_monitor=True)
def ccp_deadman(timer: func.TimerRequest) -> None:
    sub = os.environ["CCP_SUBSCRIPTION_ID"]
    dry = os.environ.get("DRY_RUN", "true").lower() in ("1", "true", "yes")
    cred = DefaultAzureCredential()
    report = evaluate(CognitiveServicesManagementClient(cred, sub), ResourceManagementClient(cred, sub),
                      datetime.now(timezone.utc), dry)
    logging.info("ccp-deadman report %s", json.dumps(report))
