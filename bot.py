"""
dune-discord-status
--------------------
Polls one or more self-hosted Dune Awakening battlegroups' status (via SSH
+ kubectl, reading the igw.funcom.com/v1 BattleGroup and ServerStats
custom resources) and keeps a self-updating Discord embed per battlegroup.

Supports multiple sietches (battlegroups), each optionally on a different
host, each posting to its own channel.

Configuration - multi-sietch (recommended):
    For each sietch, set a numbered block starting at 1:
        SIETCH_1_NAMESPACE          e.g. funcom-seabass-sh-90e2edcd77729fd-jowxys
        SIETCH_1_BATTLEGROUP_NAME   e.g. sh-90e2edcd77729fd-jowxys
        SIETCH_1_CHANNEL_ID         Discord channel to post this sietch's embed in
        SIETCH_1_SSH_HOST           optional - overrides the global SSH_HOST below
        SIETCH_1_SSH_USER           optional - overrides the global SSH_USER below
        SIETCH_1_SSH_KEY_PATH       optional - overrides the global SSH_KEY_PATH below
    ...then SIETCH_2_*, SIETCH_3_*, and so on. The bot reads them in order
    starting at 1 and stops at the first missing number.

Global settings (used as defaults for any sietch that doesn't override them,
and required if you have even one sietch relying on the default):
    DISCORD_BOT_TOKEN      - your bot's token
    SSH_HOST, SSH_USER, SSH_KEY_PATH - default SSH target
    KUBECTL_PATH            - full path to kubectl on the VM (default /usr/local/bin/kubectl)
    KUBECONFIG_PATH         - full path to kubeconfig on the VM (no "~", see README)
    POLL_INTERVAL_SECONDS   - how often to refresh (default 60)
    PLAYER_LOG_TAIL_LINES   - log lines scanned per pod for player names (default 500)

Legacy single-sietch configuration (still supported for existing
deployments): if no SIETCH_1_* vars are set, the bot falls back to the
original single-battlegroup vars: K8S_NAMESPACE, BATTLEGROUP_NAME,
DISCORD_CHANNEL_ID. Existing .env files keep working unchanged.

State (which Discord message each sietch is editing) is persisted under
/data/ so restarts don't spam new messages - mount a volume there.
"""

import asyncio
import json
import logging
import os
import re
import shlex
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import discord
import paramiko
from discord.ext import tasks

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("dune-discord-status")

# --- Global configuration ----------------------------------------------
DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
DEFAULT_SSH_HOST = os.environ.get("SSH_HOST")
DEFAULT_SSH_USER = os.environ.get("SSH_USER")
DEFAULT_SSH_KEY_PATH = os.environ.get("SSH_KEY_PATH")
KUBECTL_PATH = os.environ.get("KUBECTL_PATH", "/usr/local/bin/kubectl")
KUBECONFIG_PATH = os.environ.get("KUBECONFIG_PATH", "~/.kube/config")
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "60"))
PLAYER_LOG_TAIL_LINES = int(os.environ.get("PLAYER_LOG_TAIL_LINES", "500"))

STATE_DIR = Path("/data")
LEGACY_STATE_FILE = STATE_DIR / "message_id.txt"
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


@dataclass
class Sietch:
    key: str
    namespace: str
    battlegroup_name: str
    channel_id: int
    ssh_host: str
    ssh_user: str
    ssh_key_path: str
    is_legacy: bool = False


def _require(value: Optional[str], what: str) -> str:
    if not value:
        raise RuntimeError(
            f"Missing {what} - set it explicitly per-sietch or as a global default. See README."
        )
    return value


def load_sietches() -> list:
    sietches = []
    i = 1
    while True:
        ns = os.environ.get(f"SIETCH_{i}_NAMESPACE")
        bg = os.environ.get(f"SIETCH_{i}_BATTLEGROUP_NAME")
        ch = os.environ.get(f"SIETCH_{i}_CHANNEL_ID")
        if not (ns and bg and ch):
            break
        sietches.append(
            Sietch(
                key=str(i),
                namespace=ns,
                battlegroup_name=bg,
                channel_id=int(ch),
                ssh_host=_require(
                    os.environ.get(f"SIETCH_{i}_SSH_HOST", DEFAULT_SSH_HOST), f"SSH host for sietch {i}"
                ),
                ssh_user=_require(
                    os.environ.get(f"SIETCH_{i}_SSH_USER", DEFAULT_SSH_USER), f"SSH user for sietch {i}"
                ),
                ssh_key_path=_require(
                    os.environ.get(f"SIETCH_{i}_SSH_KEY_PATH", DEFAULT_SSH_KEY_PATH),
                    f"SSH key path for sietch {i}",
                ),
            )
        )
        i += 1

    if sietches:
        return sietches

    # Legacy single-sietch fallback, for .env files created before
    # multi-sietch support existed.
    ns = os.environ.get("K8S_NAMESPACE")
    bg = os.environ.get("BATTLEGROUP_NAME")
    ch = os.environ.get("DISCORD_CHANNEL_ID")
    if ns and bg and ch:
        log.info("Using legacy single-sietch configuration (K8S_NAMESPACE/BATTLEGROUP_NAME/DISCORD_CHANNEL_ID).")
        return [
            Sietch(
                key="legacy",
                namespace=ns,
                battlegroup_name=bg,
                channel_id=int(ch),
                ssh_host=_require(DEFAULT_SSH_HOST, "SSH_HOST"),
                ssh_user=_require(DEFAULT_SSH_USER, "SSH_USER"),
                ssh_key_path=_require(DEFAULT_SSH_KEY_PATH, "SSH_KEY_PATH"),
                is_legacy=True,
            )
        ]

    raise RuntimeError(
        "No sietch configuration found. Set SIETCH_1_NAMESPACE / SIETCH_1_BATTLEGROUP_NAME / "
        "SIETCH_1_CHANNEL_ID (and so on for more sietches), or the legacy K8S_NAMESPACE / "
        "BATTLEGROUP_NAME / DISCORD_CHANNEL_ID vars. See README.md."
    )


class SSHSession:
    """Reuses one SSH connection across several commands in a poll cycle,
    instead of reconnecting per-command."""

    def __init__(self, host: str, user: str, key_path: str):
        self._host = host
        self._user = user
        self._key_path = key_path
        self._client = None

    def __enter__(self):
        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self._client.connect(
            hostname=self._host,
            username=self._user,
            key_filename=self._key_path,
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


def fetch_battlegroup_status(ssh: SSHSession, sietch: Sietch) -> dict:
    cmd = (
        f"KUBECONFIG={shlex.quote(KUBECONFIG_PATH)} "
        f"{shlex.quote(KUBECTL_PATH)} get battlegroups {shlex.quote(sietch.battlegroup_name)} "
        f"-n {shlex.quote(sietch.namespace)} -o json"
    )
    raw = ssh.run(cmd)
    return json.loads(raw)


def fetch_server_stats(ssh: SSHSession, sietch: Sietch) -> list:
    cmd = (
        f"KUBECONFIG={shlex.quote(KUBECONFIG_PATH)} "
        f"{shlex.quote(KUBECTL_PATH)} get serverstats "
        f"-n {shlex.quote(sietch.namespace)} -o json"
    )
    raw = ssh.run(cmd)
    data = json.loads(raw)
    return data.get("items", [])


def fetch_recent_player_names(ssh: SSHSession, sietch: Sietch, pod_name: str) -> list:
    """Best-effort: greps the pod's own log tail for player login-related
    lines and returns distinct names, most-recently-seen first. This is
    NOT a live roster - a name here means "seen recently in the log
    window", not necessarily "still online right now"."""
    cmd = (
        f"KUBECONFIG={shlex.quote(KUBECONFIG_PATH)} "
        f"{shlex.quote(KUBECTL_PATH)} logs {shlex.quote(pod_name)} "
        f"-n {shlex.quote(sietch.namespace)} --tail={PLAYER_LOG_TAIL_LINES}"
    )
    try:
        raw = ssh.run(cmd)
    except Exception as ex:
        log.warning("Could not fetch logs for %s: %s", pod_name, ex)
        return []

    seen: "OrderedDict[str, str]" = OrderedDict()
    for match in PLAYER_LOG_PATTERN.finditer(raw):
        name, fls_id = match.group(1), match.group(2)
        if fls_id in seen:
            del seen[fls_id]
        seen[fls_id] = name

    return list(reversed(seen.values()))


def build_embed(bg: dict, stats: list, player_names_by_pod: dict, fallback_title: str) -> discord.Embed:
    title = bg.get("spec", {}).get("title", fallback_title)
    status = bg.get("status", {})
    phase = status.get("phase", "Unknown")

    phase_colors = {
        "Running": discord.Color.green(),
        "Reconciling": discord.Color.gold(),
    }
    color = phase_colors.get(phase, discord.Color.red())

    embed = discord.Embed(title=f"\U0001f3dc\ufe0f {title}", color=color)
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
        emoji = "\U0001f7e2" if phase_val == "Healthy" else "\U0001f7e1" if phase_val else "\U0001f534"
        health_bits.append(f"{emoji} {name}")
    mq = utilities.get("messageQueues", {})
    if mq.get("healthy"):
        health_bits.append(f"\U0001f7e2 Message Queues ({mq['healthy']})")
    embed.add_field(name="Services", value="\n".join(health_bits) or "Unknown", inline=False)

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
        status_emoji = "\U0001f7e2" if ready else "\U0001f7e1"

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
    def __init__(self, sietches: list):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.sietches = sietches
        self.message_ids: dict = {}

    def _state_file(self, sietch: Sietch) -> Path:
        if sietch.is_legacy and LEGACY_STATE_FILE.exists():
            return LEGACY_STATE_FILE
        return STATE_DIR / f"message_id_{sietch.key}.txt"

    async def setup_hook(self):
        for sietch in self.sietches:
            state_file = self._state_file(sietch)
            if state_file.exists():
                try:
                    self.message_ids[sietch.key] = int(state_file.read_text().strip())
                except ValueError:
                    self.message_ids[sietch.key] = None
            else:
                self.message_ids[sietch.key] = None
        self.poll_loop.start()

    async def on_ready(self):
        log.info("Logged in as %s, tracking %d sietch(es)", self.user, len(self.sietches))

    def _fetch_one(self, sietch: Sietch):
        with SSHSession(sietch.ssh_host, sietch.ssh_user, sietch.ssh_key_path) as ssh:
            bg_data = fetch_battlegroup_status(ssh, sietch)
            stats = fetch_server_stats(ssh, sietch)
            player_names_by_pod = {}
            for item in stats:
                if item.get("status", {}).get("runtime", {}).get("players", 0) > 0:
                    pod_name = item.get("metadata", {}).get("name", "")
                    if pod_name:
                        player_names_by_pod[pod_name] = fetch_recent_player_names(ssh, sietch, pod_name)
            return bg_data, stats, player_names_by_pod

    async def _update_sietch(self, sietch: Sietch):
        try:
            bg_data, stats, player_names_by_pod = await asyncio.to_thread(self._fetch_one, sietch)
            embed = build_embed(bg_data, stats, player_names_by_pod, sietch.battlegroup_name)
        except Exception as ex:
            log.error("[%s] Failed to fetch/parse status: %s", sietch.key, ex)
            embed = discord.Embed(
                title=f"\u26a0\ufe0f Status check failed ({sietch.battlegroup_name})",
                description=f"```{ex}```",
                color=discord.Color.red(),
            )
            embed.timestamp = datetime.now(timezone.utc)

        channel = self.get_channel(sietch.channel_id) or await self.fetch_channel(sietch.channel_id)
        state_file = self._state_file(sietch)

        try:
            message_id = self.message_ids.get(sietch.key)
            if message_id:
                try:
                    message = await channel.fetch_message(message_id)
                    await message.edit(embed=embed)
                    return
                except discord.NotFound:
                    log.warning("[%s] Previous status message not found, sending a new one.", sietch.key)
                    self.message_ids[sietch.key] = None

            message = await channel.send(embed=embed)
            self.message_ids[sietch.key] = message.id
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            state_file.write_text(str(message.id))
        except Exception as ex:
            log.error("[%s] Failed to send/edit Discord message: %s", sietch.key, ex)

    @tasks.loop(seconds=POLL_INTERVAL_SECONDS)
    async def poll_loop(self):
        for sietch in self.sietches:
            await self._update_sietch(sietch)

    @poll_loop.before_loop
    async def before_poll_loop(self):
        await self.wait_until_ready()


if __name__ == "__main__":
    sietches = load_sietches()
    client = DuneStatusClient(sietches)
    client.run(DISCORD_BOT_TOKEN)
