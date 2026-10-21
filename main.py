"""IncidentPilot command line.

    python main.py scenarios                                   list the recorded incidents
    python main.py run bad_deploy_checkout --demo              run one incident in the terminal (asks you to approve)
    python main.py run bad_deploy_checkout --demo --auto-approve
    python main.py serve --demo                                start the API and the UI (http://127.0.0.1:8000)

Drop --demo to use a real LLM (set ANTHROPIC_API_KEY or OPENAI_API_KEY in .env).
"""
import argparse
import logging
import os
import time

from incidentpilot.config import Settings
from incidentpilot.demo_llm import DemoLLM
from incidentpilot.llm import FallbackLLM, describe_llm, get_llm
from incidentpilot.service import build_service, run_to_end
from incidentpilot.tools import ScenarioTools


def auto_approver(approval):
    """Approve everything the policy allowed. Only for demos."""
    return {"approved": True, "reviewer": "auto-approve", "comment": ""}


def terminal_approver(approval):
    """Show the proposed change and let a person decide in the terminal."""
    print("")
    print("=== APPROVAL NEEDED ===")
    print("Proposed change : " + approval["action"] + " " + str(approval["arguments"]) + " on " + approval["service"])
    print("Reason          : " + approval["reason"])
    answer = input("Approve this change? [y/N] ").strip().lower()
    return {"approved": answer == "y", "reviewer": "terminal user", "comment": ""}


def choose_llm(settings, use_demo, demo_delay=0.0):
    if use_demo:
        return DemoLLM(delay_seconds=demo_delay)
    return get_llm(settings)


def server_llm(settings, use_demo):
    """The model the web server uses when a visitor does not bring their own key.

    --demo: always the scripted model. Otherwise: the configured model (API key in the environment),
    falling back to the scripted model for an incident whose first call fails (quota, network).
    No key at all: the scripted model, so the demo still works.
    """
    scripted = DemoLLM(delay_seconds=0.6)      # waits a little per "call", so you can watch the steps
    if use_demo:
        return scripted
    try:
        real = get_llm(settings)
    except RuntimeError as error:
        print("No LLM configured (" + str(error).split(".")[0] + "). Using the scripted model.")
        return scripted
    return FallbackLLM(real, scripted)


def check_llm_command(args):
    """Send one tiny prompt to the configured model: a 5-second check that the key and model work."""
    settings = Settings()
    llm = get_llm(settings)
    start = time.time()
    reply = llm.complete("Reply with ONE JSON object and nothing else.", 'Reply with {"ok": true}.')
    print("model : " + describe_llm(llm))
    print("time  : " + str(round(time.time() - start, 2)) + "s")
    print("reply : " + reply.strip()[:200])


def run_command(args):
    settings = Settings()
    service = build_service(settings, choose_llm(settings, args.demo), verbose=True)
    print("LLM: " + type(service.llm).__name__ + " | retrieval: " + service.retriever.describe())

    incident = service.trigger_scenario(args.scenario)
    if args.auto_approve:
        approver = auto_approver
    else:
        approver = terminal_approver
    final = run_to_end(service, incident["id"], approver)

    report_path = os.path.join(settings.reports_dir, args.scenario + "_" + final["id"] + ".md")
    with open(report_path, "w", encoding="utf-8") as handle:
        handle.write(final["state"].get("report", ""))
    print("")
    print("Final status: " + final["status"].upper())
    print("Report saved: " + report_path)


def serve_command(args):
    import uvicorn
    from incidentpilot.api import create_app

    settings = Settings()
    service = build_service(settings, server_llm(settings, args.demo))
    stuck = service.mark_interrupted()
    if stuck > 0:
        print(str(stuck) + " incident(s) were running when the server stopped. Retry them from the UI or the API.")
    print("LLM: " + describe_llm(service.llm) + " | retrieval: " + service.retriever.describe())
    uvicorn.run(create_app(service), host=settings.host, port=settings.port)


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)      # hides the Hugging Face model download checks
    parser = argparse.ArgumentParser(description="IncidentPilot: an LLM agent that investigates incidents.")
    commands = parser.add_subparsers(dest="command")

    commands.add_parser("scenarios", help="list the recorded scenarios")

    run_parser = commands.add_parser("run", help="run one incident in the terminal")
    run_parser.add_argument("scenario")
    run_parser.add_argument("--demo", action="store_true", help="use the scripted demo model (no API key)")
    run_parser.add_argument("--auto-approve", action="store_true", help="approve automatically")

    serve_parser = commands.add_parser("serve", help="start the API and the UI")
    serve_parser.add_argument("--demo", action="store_true", help="always use the scripted demo model")

    commands.add_parser("check-llm", help="send one test prompt to the configured model")

    args = parser.parse_args()
    if args.command == "scenarios":
        settings = Settings()
        for name in ScenarioTools(settings.scenarios_dir).list_scenarios():
            print(name)
    elif args.command == "run":
        run_command(args)
    elif args.command == "serve":
        serve_command(args)
    elif args.command == "check-llm":
        check_llm_command(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
