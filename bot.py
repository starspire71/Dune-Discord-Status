"""
dune-discord-status
--------------------
Polls a self-hosted Dune Awakening battlegroup's status (via SSH + kubectl,
reading the igw.funcom.com/v1 BattleGroup and ServerStats custom resources)
and keeps a single Discord embed updated with server health and per-map
player counts.

Required environment variables:
    DISCORD_BOT_TOKEN   - your bot's token
    DISCORD_CHANNEL_ID  - channel to post/update the status embed in
    SSH_HOST            - IP/hostname of the Dune Awakening VM
    SSH_USER            - SSH username (e.g. "dune")
    SSH_KEY_PATH        - path to the private key (mounted into the container)
    K8S_NAMESPACE       - e.g. funcom-seabass-sh-90e2edcd77729fd-jowxys
    BATTLEGROUP_NAME    - e.g. sh-90e2edcd77729fd-jowxys
    KUBECTL_PATH        - full path to kubectl on the VM (default /usr/local/bin/kubectl)
    KUBECONFIG_PATH     - path to kubeconfig on the VM (default ~/.kube/config)
    POLL_INTERVAL_SECONDS - how often to refresh (default 60)

State (the Discord message ID being edited) is persisted to
/data/message_id.txt so restarts don't spam a new message every time -
mount a volume at /data to keep this across container restarts.
"""

import asyncio
import json
import logging
import os
import re
import shlex
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

import discord
import paramiko
from discord.ext import tasks

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("dune-discord-status")

# --- Configuration ----------------------------------------------------
DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
DISCORD_CHANNEL_ID = int(os.environ["DISCORD_CHANNEL_ID"])
SSH_HOST = os.environ["SSH_HOST"]
SSH_USER = os.environ["SSH_USER"]
SSH_KEY_PATH = os.environ["SSH_KEY_PATH"]
K8S_NAMESPACE = os.environ["K8S_NAMESPACE"]
BATTLEGROUP_NAME = os.environ["BATTLEGROUP_NAME"]
KUBECTL_PATH = os.environ.get("KUBECTL_PATH", "/usr/local/bin/kubectl")
KUBECONFIG_PATH = os.environ.get("KUBECONFIG_PATH", "~/.kube/config")
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "60"))
PLAYER_LOG_TAIL_LINES = int(os.environ.get("PLAYER_LOG_TAIL_LINES", "500"))

STATE_FILE = Path("/data/message_id.txt")
# ------------------------------------------------------------------------

# Friendlier display names for map internal names. Extend as needed -
# anything not listed here just falls back to the raw internal name.
MAP_DISPLAY_NAMES = {
    "Survival_1": "Hagga Basin",
    "Overmap": "Overmap",
    "DeepDesert_1": "Deep Desert",
    "SH_Arrakeen": "Arrakeen",
    "SH_HarkoVillage": "Harko Village",
}

# Matches lines like:
#   ExitPreInGameState for player Starspire (FLS: 90E2EDCD77729FD)
#   LoadingInvulnerability removed for player Starspire (FLS: 90E2EDCD77729FD)
# This is inferred from log text, not a structured API - see README for
# the "recently seen" caveat this implies.
PLAYER_LOG_PATTERN = re.compile(r"for player (\S+) \(FLS: (\S+)\)")


class SSHSession:
    """Reuses one SSH connection across several commands in a poll cycle,
    instead of reconnecting per-command."""

    def __init__(self):
        self._client = None

    def __enter__(self):
        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self._client.connect(
            hostname=SSH_HOST,
            username=SSH_USER,
            key_filename=SSH_KEY_PATH,
            timeout=15,
        )
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._client:
            self._client.close()

    def run(self, command: str) -> str:
        stdin, stdout, stderr = self._client.exec_command(command, timeout=30)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        exit_status = stdout.channel.recv_exit_status()
        if exit_status != 0:
            raise RuntimeError(f"remote command failed (exit {exit_status}): {err.strip()}")
        return out


def fetch_battlegroup_status(ssh: SSHSession) -> dict:
    cmd = (
        f"KUBECONFIG={shlex.quote(KUBECONFIG_PATH)} "
        f"{shlex.quote(KUBECTL_PATH)} get battlegroups {shlex.quote(BATTLEGROUP_NAME)} "
        f"-n {shlex.quote(K8S_NAMESPACE)} -o json"
    )
    raw = ssh.run(cmd)
    return json.loads(raw)


def fetch_server_stats(ssh: SSHSession) -> list:
    cmd = (
        f"KUBECONFIG={shlex.quote(KUBECONFIG_PATH)} "
        f"{shlex.quote(KUBECTL_PATH)} get serverstats "
        f"-n {shlex.quote(K8S_NAMESPACE)} -o json"
    )
    raw = ssh.run(cmd)
    data = json.loads(raw)
    return data.get("items", [])


def fetch_recent_player_names(ssh: SSHSession, pod_name: str) -> list:
    """Best-effort: greps the pod's own log tail for player login-related
    lines and returns distinct names, most-recently-seen first. This is
    NOT a live roster - a name here means "seen recently in the log
    window", not necessarily "still online right now"."""
    cmd = (
        f"KUBECONFIG={shlex.quote(KUBECONFIG_PATH)} "
        f"{shlex.quote(KUBECTL_PATH)} logs {shlex.quote(pod_name)} "
        f"-n {shlex.quote(K8S_NAMESPACE)} --tail={PLAYER_LOG_TAIL_LINES}"
    )
    try:
        raw = ssh.run(cmd)
    except Exception as ex:
        log.warning("Could not fetch logs for %s: %s", pod_name, ex)
        return []

    # OrderedDict keyed by FLS id, re-inserted (moved to end) on every
    # match so the final order reflects most-recently-seen last.
    seen: "OrderedDict[str, str]" = OrderedDict()
    for match in PLAYER_LOG_PATTERN.finditer(raw):
        name, fls_id = match.group(1), match.group(2)
        if fls_id in seen:
            del seen[fls_id]
        seen[fls_id] = name

    return list(reversed(seen.values()))


def build_embed(bg: dict, stats: list, player_names_by_pod: dict) -> discord.Embed:
    title = bg.get("spec", {}).get("title", BATTLEGROUP_NAME)
    status = bg.get("status", {})
    phase = status.get("phase", "Unknown")

    phase_colors = {
        "Running": discord.Color.green(),
        "Reconciling": discord.Color.gold(),
    }
    color = phase_colors.get(phase, discord.Color.red())

    embed = discord.Embed(title=f"🏜️ {title}", color=color)
    embed.add_field(name="Status", value=phase, inline=True)

    start_ts = status.get("startTimestamp")
    if start_ts:
        started = datetime.fromisoformat(start_ts.replace("Z", "+00:00"))
        elapsed = datetime.now(timezone.utc) - started
        hours = int(elapsed.total_seconds() // 3600)
        minutes = int((elapsed.total_seconds() % 3600) // 60)
        embed.add_field(name="Uptime", value=f"{hours}h {minutes}m", inline=True)

    utilities = status.get("utilities", {})
    health_bits = []
    for name, key in (("Director", "director"), ("Gateway", "serverGateway"), ("Text Router", "textRouter")):
        phase_val = utilities.get(key, {}).get("phase", "?")
        emoji = "🟢" if phase_val == "Healthy" else "🟡" if phase_val else "🔴"
        health_bits.append(f"{emoji} {name}")
    mq = utilities.get("messageQueues", {})
    if mq.get("healthy"):
        health_bits.append(f"🟢 Message Queues ({mq['healthy']})")
    embed.add_field(name="Services", value="\n".join(health_bits) or "Unknown", inline=False)

    # Per-map player counts (+ best-effort recently-seen names), only for
    # maps that currently have a running pod
    total_players = 0
    map_lines = []
    for item in stats:
        pod_name = item.get("metadata", {}).get("name", "")
        map_name = item.get("spec", {}).get("area", {}).get("map", "?")
        runtime = item.get("status", {}).get("runtime", {})
        players = runtime.get("players", 0)
        ready = runtime.get("ready", False)
        total_players += players
        display_name = MAP_DISPLAY_NAMES.get(map_name, map_name)
        status_emoji = "🟢" if ready else "🟡"

        if players > 0:
            names = player_names_by_pod.get(pod_name, [])[:players]
            if names:
                map_lines.append(
                    f"{status_emoji} **{display_name}**: {players} player(s) - {', '.join(names)} *(recently seen)*"
                )
            else:
                map_lines.append(f"{status_emoji} **{display_name}**: {players} player(s)")
        else:
            map_lines.append(f"{status_emoji} **{display_name}**: 0 players")

    if map_lines:
        embed.add_field(name=f"Maps ({total_players} total players)", value="\n".join(map_lines), inline=False)

    embed.set_footer(text="Updated - player names are inferred from recent log activity, not a live roster")
    embed.timestamp = datetime.now(timezone.utc)
    return embed


class DuneStatusClient(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.message_id: int | None = None

    async def setup_hook(self):
        if STATE_FILE.exists():
            try:
                self.message_id = int(STATE_FILE.read_text().strip())
            except ValueError:
                self.message_id = None
        self.poll_loop.start()

    async def on_ready(self):
        log.info("Logged in as %s", self.user)

    def _fetch_all(self):
        """Runs all SSH-dependent fetches on one reused connection."""
        with SSHSession() as ssh:
            bg_data = fetch_battlegroup_status(ssh)
            stats = fetch_server_stats(ssh)
            player_names_by_pod = {}
            for item in stats:
                if item.get("status", {}).get("runtime", {}).get("players", 0) > 0:
                    pod_name = item.get("metadata", {}).get("name", "")
                    if pod_name:
                        player_names_by_pod[pod_name] = fetch_recent_player_names(ssh, pod_name)
            return bg_data, stats, player_names_by_pod

    @tasks.loop(seconds=POLL_INTERVAL_SECONDS)
    async def poll_loop(self):
        try:
            bg_data, stats, player_names_by_pod = await asyncio.to_thread(self._fetch_all)
            embed = build_embed(bg_data, stats, player_names_by_pod)
        except Exception as ex:
            log.error("Failed to fetch/parse status: %s", ex)
            embed = discord.Embed(
                title="⚠️ Status check failed",
                description=f"```{ex}```",
                color=discord.Color.red(),
            )
            embed.timestamp = datetime.now(timezone.utc)

        channel = self.get_channel(DISCORD_CHANNEL_ID) or await self.fetch_channel(DISCORD_CHANNEL_ID)

        try:
            if self.message_id:
                try:
                    message = await channel.fetch_message(self.message_id)
                    await message.edit(embed=embed)
                    return
                except discord.NotFound:
                    log.warning("Previous status message not found, sending a new one.")
                    self.message_id = None

            message = await channel.send(embed=embed)
            self.message_id = message.id
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(str(message.id))
        except Exception as ex:
            log.error("Failed to send/edit Discord message: %s", ex)

    @poll_loop.before_loop
    async def before_poll_loop(self):
        await self.wait_until_ready()


if __name__ == "__main__":
    client = DuneStatusClient()
    client.run(DISCORD_BOT_TOKEN)
