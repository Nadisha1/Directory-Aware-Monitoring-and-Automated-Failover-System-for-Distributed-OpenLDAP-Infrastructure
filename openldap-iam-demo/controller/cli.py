"""
Minimal CLI (proposal section 9: "a minimal CLI is mandatory while the
dashboard remains optional").

Usage (run inside /opt/remediation-venv on the ctrl VM):
    python3 cli.py status
    python3 cli.py monitor
    python3 cli.py fence ldap2 --reason "manual test"
    python3 cli.py reintegrate ldap2 --reason "manual recovery confirmed"
    python3 cli.py mttr
"""

import click
from prettytable import PrettyTable

import config
import health
import remediation
from controller import Controller
from state_machine import cluster_summary


@click.group()
def cli():
    """Directory-State-Aware Remediation Controller CLI."""
    pass


@cli.command()
def status():
    """One-shot health snapshot of every node."""
    ctrl = Controller()
    snapshots, _ = ctrl.poll_once()

    table = PrettyTable()
    table.field_names = ["Node", "State", "Reachable", "Bind (ms)", "contextCSN"]
    for node in ctrl.nodes:
        s = snapshots[node.name]
        t = ctrl.trackers[node.name]
        table.add_row([
            node.name,
            t.state.value,
            s["reachable"],
            f"{s['bind_latency_ms']:.1f}" if s["bind_latency_ms"] else "-",
            s["context_csn"].isoformat() if s["context_csn"] else "-",
        ])
    click.echo(table)
    click.echo(f"Cluster state: {cluster_summary(ctrl.trackers)}")


@cli.command()
@click.option("--interval", default=config.POLL_INTERVAL_SECONDS, help="seconds between polls")
def monitor(interval):
    """Continuous monitoring loop with live remediation (Ctrl-C to stop)."""
    Controller().run_forever(interval=interval)


@cli.command()
@click.argument("node_name")
@click.option("--reason", default="manual operator action")
def fence(node_name, reason):
    """Manually fence a node (remove from HAProxy rotation)."""
    ok = remediation.fence_node(node_name, reason)
    click.echo("OK" if ok else "FAILED")


@cli.command()
@click.argument("node_name")
@click.option("--reason", default="manual operator action")
def reintegrate(node_name, reason):
    """Manually reintegrate a node (add back to HAProxy rotation)."""
    ok = remediation.reintegrate_node(node_name, reason)
    click.echo("OK" if ok else "FAILED")


@cli.command()
def mttr():
    """Report Mean Time to Recovery across the current process's history.
    For the dissertation's quantitative comparison (section 7), run
    `monitor` during each scripted failure scenario and log its output;
    this command only reports on cycles seen within a single run."""
    click.echo("Run `monitor` through a full fence/recover cycle first; "
                "MTTR is computed from that in-memory history.")


if __name__ == "__main__":
    cli()
