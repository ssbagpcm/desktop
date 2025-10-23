#!/usr/bin/env python3

import json
import os
import sys
import subprocess
import shutil
import socket
import re
import getpass
from datetime import datetime, timezone

STATE_FILE = "storages.json"
IMAGE_NAME = "vnc-desktop"
DOCKERFILE_PATH = "."
CONTAINER_PREFIX = "vnc-"  # all our containers are named vnc-<storage>


class StorageManager:
    def __init__(self):
        self.state = self.load_state()

    def load_state(self):
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, 'r') as f:
                return json.load(f)
        return {"storages": {}, "image_built": False}

    def save_state(self):
        with open(STATE_FILE, 'w') as f:
            json.dump(self.state, f, indent=2)

    def ensure_image(self):
        result = subprocess.run(['docker', 'images', '-q', IMAGE_NAME],
                                capture_output=True, text=True)
        if not result.stdout.strip():
            print(f"Building image {IMAGE_NAME}...")
            build_result = subprocess.run(
                ['docker', 'build', '-t', IMAGE_NAME, DOCKERFILE_PATH]
            )
            if build_result.returncode != 0:
                print("Error building image")
                sys.exit(1)
            self.state["image_built"] = True
            self.save_state()
            print("Image built successfully")

    def create(self, name):
        if name in self.state["storages"]:
            print(f"Storage '{name}' already exists")
            return

        storage_path = os.path.abspath(f"storages/{name}")
        os.makedirs(storage_path, exist_ok=True)

        self.state["storages"][name] = {
            "path": storage_path,
            "container_id": None,
            "status": "stopped",
            "port": None
        }
        self.save_state()
        print(f"Storage '{name}' created at {storage_path}")

    def is_port_in_use_system(self, port: int) -> bool:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", port))
            s.close()
            return False
        except OSError:
            return True

    def is_port_in_use_docker(self, port: int) -> bool:
        result = subprocess.run(
            ['docker', 'ps', '--format', '{{.Ports}}'],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            return False
        for line in result.stdout.splitlines():
            if f":{port}->" in line:
                return True
        return False

    def is_port_available(self, port: int) -> bool:
        return (not self.is_port_in_use_system(port)) and (not self.is_port_in_use_docker(port))

    def find_next_free_port(self, start: int = 2000, end: int = 65535) -> int:
        for p in range(start, end + 1):
            if self.is_port_available(p):
                return p
        return -1

    def prompt_port_with_fallback(self, requested: int) -> int:
        if self.is_port_available(requested):
            return requested

        nxt = self.find_next_free_port(requested + 1)
        if nxt == -1:
            print("No free ports found.")
            return -1

        while True:
            ans = input(f"Port {requested} is already in use. Use {nxt} instead? (y/n): ").lower().strip()
            if ans in ("y", "yes"):
                return nxt
            elif ans in ("n", "no"):
                custom = input("Enter a port number (1024-65535), or 'auto' to pick the next free: ").strip().lower()
                if custom == "auto":
                    auto = self.find_next_free_port(2000)
                    if auto == -1:
                        print("No free ports available.")
                        return -1
                    print(f"Using port {auto}")
                    return auto
                else:
                    try:
                        cp = int(custom)
                        if 1024 <= cp <= 65535:
                            if self.is_port_available(cp):
                                return cp
                            else:
                                print(f"Port {cp} is in use. Try again.")
                                continue
                        else:
                            print("Invalid range. Must be between 1024 and 65535.")
                    except ValueError:
                        print("Invalid input. Try again.")
            else:
                print("Please answer y or n.")

    def write_init_script(self, storage_path: str):
        # Overlay root with tmpfs for /tmp and /run inside the chroot (avoid RO /tmp issues)
        init_script = r"""#!/bin/bash
set -euo pipefail

MODE="vnc"
if [[ "${1:-}" == "terminal" ]]; then
  MODE="terminal"
fi

# Ensure dirs
mkdir -p /overlay/lower /overlay/merged
mkdir -p /storage/upper /storage/work

# Prepare a RO view of the current root outside of /storage
mountpoint -q /overlay/lower || mount --bind / /overlay/lower
mount -o remount,ro /overlay/lower || true

# Mount overlay
mount -t overlay overlay -o lowerdir=/overlay/lower,upperdir=/storage/upper,workdir=/storage/work /overlay/merged

# Bind critical virtual filesystems (rbind to preserve nested mounts like devpts)
for m in proc sys dev; do
  mkdir -p "/overlay/merged/$m"
  mount --rbind "/$m" "/overlay/merged/$m"
  mount --make-rslave "/overlay/merged/$m"
done

# Ensure devpts inside chroot (for terminals/PTY)
mkdir -p /overlay/merged/dev/pts
if ! mountpoint -q /overlay/merged/dev/pts; then
  mount -t devpts devpts /overlay/merged/dev/pts -o mode=0620,ptmxmode=0666,gid=5 || mount -t devpts devpts /overlay/merged/dev/pts || true
fi
# Ensure /dev/ptmx points to /dev/pts/ptmx if missing
if [ ! -e /overlay/merged/dev/ptmx ]; then
  ln -s pts/ptmx /overlay/merged/dev/ptmx || true
fi

# Fresh tmpfs for /run and /tmp inside the chroot (writable)
mkdir -p /overlay/merged/run /overlay/merged/tmp
mount -t tmpfs -o mode=755 tmpfs /overlay/merged/run
mount -t tmpfs -o mode=1777 tmpfs /overlay/merged/tmp
mkdir -p /overlay/merged/tmp/.X11-unix
chmod 1777 /overlay/merged/tmp /overlay/merged/tmp/.X11-unix

# Ensure dev/shm exists
mkdir -p /overlay/merged/dev/shm

# Ensure network connectivity (DNS) inside the chroot
mkdir -p /overlay/merged/etc
cp /etc/resolv.conf /overlay/merged/etc/resolv.conf

if [[ "$MODE" == "terminal" ]]; then
  exec chroot /overlay/merged /bin/bash
else
  exec chroot /overlay/merged /usr/local/bin/entrypoint.sh
fi
"""
        init_path = f"{storage_path}/init.sh"
        # ensure storage directory exists (fix FileNotFoundError)
        os.makedirs(storage_path, exist_ok=True)
        with open(init_path, 'w') as f:
            f.write(init_script)
        os.chmod(init_path, 0o755)

    def start(self, name, port_or_mode="2000"):
        container_name = f'{CONTAINER_PREFIX}{name}'
        subprocess.run(['docker', 'stop', container_name],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(['docker', 'rm', container_name],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        self.ensure_image()

        if name not in self.state["storages"]:
            print(f"Storage '{name}' does not exist, creating it.")
            self.create(name)

        storage = self.state["storages"][name]
        storage_path = storage["path"]

        # Ensure storage path exists
        if not os.path.isdir(storage_path):
            print(f"Storage path missing, recreating: {storage_path}")
            os.makedirs(storage_path, exist_ok=True)

        self.write_init_script(storage_path)

        # Terminal mode
        if port_or_mode == "terminal":
            print(f"Starting '{name}' in terminal mode with persistent overlay...")

            cmd = [
                'docker', 'run', '--rm', '-it', '--privileged',
                '-v', f'{storage_path}:/storage',
                '--name', container_name,
                '--hostname', container_name,
                '--entrypoint', '/storage/init.sh',
                '-e', 'VNC_SECURITY_TYPES=None',
                IMAGE_NAME, 'terminal'
            ]
            subprocess.run(cmd)
            return

        # VNC mode: choose host port
        try:
            requested_port = int(port_or_mode)
        except ValueError:
            print(f"Invalid port: {port_or_mode}")
            return

        chosen_port = self.prompt_port_with_fallback(requested_port)
        if chosen_port == -1:
            print("Unable to choose a port. Aborting.")
            return

        print(f"Starting '{name}' on host port {chosen_port} (container port 5901) with persistent overlay...")

        env = ['-e', 'VNC_SECURITY_TYPES=None', '-e', 'PORT=5901']
        publish = f'0.0.0.0:{chosen_port}:5901'

        cmd = [
            'docker', 'run', '-d', '--privileged',
            '-p', publish,
            '-v', f'{storage_path}:/storage',
            '--name', container_name,
            '--hostname', container_name,
            '--entrypoint', '/storage/init.sh',
        ] + env + [
            IMAGE_NAME
        ]

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"Error starting container: {result.stderr.strip()}")
            return

        container_id = result.stdout.strip()
        storage["container_id"] = container_id
        storage["status"] = "running"
        storage["port"] = chosen_port
        self.save_state()

        print(f"Storage '{name}' started successfully")
        print(f"Container ID: {container_id[:12]}")
        print(f"VNC: localhost:{chosen_port}")
        print("")
        print(f"All changes persist in: {storage_path}/upper (overlay)")
        print(f"Use 'python main.py stop {name}' to stop")

    def stop(self, name):
        if name not in self.state["storages"]:
            print(f"Storage '{name}' does not exist")
            return

        storage = self.state["storages"][name]
        container_id = storage.get("container_id")
        if not container_id:
            print(f"Storage '{name}' is not running")
            return

        container_name = f'{CONTAINER_PREFIX}{name}'
        print(f"Stopping '{name}'...")
        subprocess.run(['docker', 'exec', container_name, 'sync'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(['docker', 'rm', '-f', container_name], capture_output=True)

        storage["container_id"] = None
        storage["status"] = "stopped"
        storage["port"] = None
        self.save_state()
        print(f"Storage '{name}' stopped")

    def rename(self, old_new):
        if ':' not in old_new:
            print("Invalid format. Use: python main.py rename oldname:newname")
            return

        old_name, new_name = old_new.split(':', 1)

        if old_name not in self.state["storages"]:
            print(f"Storage '{old_name}' does not exist")
            return

        if new_name in self.state["storages"]:
            print(f"Storage '{new_name}' already exists")
            return

        storage = self.state["storages"][old_name]

        if storage["status"] == "running":
            print(f"Stopping '{old_name}' before renaming...")
            self.stop(old_name)

        old_path = storage["path"]
        new_path = os.path.abspath(f"storages/{new_name}")
        if os.path.exists(old_path):
            shutil.move(old_path, new_path)

        storage["path"] = new_path
        self.state["storages"][new_name] = storage
        del self.state["storages"][old_name]
        self.save_state()

        print(f"Storage renamed from '{old_name}' to '{new_name}'")

    def delete(self, name):
        if name not in self.state["storages"]:
            print(f"Storage '{name}' does not exist")
            return

        storage = self.state["storages"][name]
        running = storage.get("status") == "running"
        container_name = f'{CONTAINER_PREFIX}{name}'

        confirmation = input("Type 'DELETE' to permanently delete: ")
        if confirmation != "DELETE":
            print("Deletion cancelled")
            return

        if running:
            print(f"Force deleting running storage '{name}' (no save)...")
            subprocess.run(['docker', 'rm', '-f', container_name],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        storage_path = storage["path"]
        if os.path.exists(storage_path):
            print(f"Deleting {storage_path}...")
            shutil.rmtree(storage_path, ignore_errors=True)

        del self.state["storages"][name]
        self.save_state()
        print(f"Storage '{name}' deleted permanently")

    # ==== LIST (advanced) ====
    def _parse_hsize_to_bytes(self, s: str) -> int:
        s = s.strip()
        m = re.match(r'^([\d\.]+)\s*([KMGTP]?i?B)$', s, re.IGNORECASE)
        if not m:
            s = s.split('/')[0].strip()
            m = re.match(r'^([\d\.]+)\s*([KMGTP]?i?B)$', s, re.IGNORECASE)
            if not m:
                try:
                    return int(float(s))
                except Exception:
                    return 0
        val = float(m.group(1))
        unit = m.group(2).lower()
        mult = {
            'b': 1,
            'kb': 1000,
            'kib': 1024,
            'mb': 1000**2,
            'mib': 1024**2,
            'gb': 1000**3,
            'gib': 1024**3,
            'tb': 1000**4,
            'tib': 1024**4,
            'pb': 1000**5,
            'pib': 1024**5,
        }.get(unit, 1)
        return int(val * mult)

    def _format_bytes(self, n: int) -> str:
        for unit in ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB']:
            if n < 1024 or unit == 'PiB':
                return f"{n:.2f} {unit}"
            n /= 1024.0

    def _dir_size_bytes(self, path: str) -> int:
        try:
            r = subprocess.run(['du', '-sb', path], capture_output=True, text=True)
            if r.returncode == 0:
                return int(r.stdout.split()[0])
        except Exception:
            pass
        total = 0
        for root, dirs, files in os.walk(path):
            for f in files:
                try:
                    fp = os.path.join(root, f)
                    total += os.path.getsize(fp)
                except OSError:
                    pass
        return total

    def _docker_stats_map(self):
        cmd = [
            'docker', 'stats', '--no-stream',
            '--format', '{{.ID}}|{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}|{{.NetIO}}|{{.BlockIO}}|{{.PIDs}}'
        ]
        r = subprocess.run(cmd, capture_output=True, text=True)
        out = {}
        if r.returncode != 0:
            return out
        for line in r.stdout.splitlines():
            parts = line.split('|')
            if len(parts) != 7:
                continue
            cid, name, cpu, mem, netio, blockio, pids = parts
            out[name.strip()] = {
                "id": cid.strip(),
                "cpu": cpu.strip(),
                "mem": mem.strip(),
                "netio": netio.strip(),
                "blockio": blockio.strip(),
                "pids": pids.strip()
            }
        return out

    def _docker_inspect(self, name_or_id: str):
        r = subprocess.run(['docker', 'inspect', name_or_id], capture_output=True, text=True)
        if r.returncode != 0:
            return None
        try:
            data = json.loads(r.stdout)
            if data:
                return data[0]
        except Exception:
            pass
        return None

    def list_storages(self):
        storages = self.state.get("storages", {})
        stats_map = self._docker_stats_map()

        if not storages:
            print("No storages found")
            print("Create one with: python main.py create <name>")
            return

        total_mem_bytes = 0
        total_disk_bytes = 0
        running_count = 0

        print("\n" + "="*80)
        print("STORAGES (managed)")
        print("="*80)

        for name, storage in storages.items():
            status = storage.get("status", "-")
            port = storage.get("port", None)
            c_id = storage.get("container_id", None)
            storage_path = storage.get("path", "-")
            container_name = f"{CONTAINER_PREFIX}{name}"

            upper_path = os.path.join(storage_path, "upper")
            upper_size = self._dir_size_bytes(upper_path) if os.path.exists(upper_path) else 0
            total_disk_bytes += upper_size

            print(f"\n{name}")
            print(f"  Status      : {status}")
            if port:
                print(f"  Port        : {port} (VNC: localhost:{port})")
            else:
                print(f"  Port        : -")
            if c_id:
                print(f"  Container   : {c_id[:12]}")
            else:
                print(f"  Container   : -")
            print(f"  Path        : {storage_path}")
            print(f"  Overlay size: {self._format_bytes(upper_size)}")

            if status == "running":
                running_count += 1
                stat = stats_map.get(container_name)
                if stat:
                    mem_used_part = stat["mem"].split('/')[0].strip()
                    mem_used_bytes = self._parse_hsize_to_bytes(mem_used_part)
                    total_mem_bytes += mem_used_bytes

                    insp = self._docker_inspect(container_name)
                    uptime_str = "-"
                    if insp and "State" in insp and insp["State"].get("Running"):
                        started = insp["State"].get("StartedAt")
                        try:
                            dt = datetime.fromisoformat(started.replace('Z', '+00:00'))
                            delta = datetime.now(timezone.utc) - dt
                            secs = int(delta.total_seconds())
                            d = secs // 86400
                            h = (secs % 86400) // 3600
                            m = (secs % 3600) // 60
                            s = secs % 60
                            uptime_str = f"{d}d {h}h {m}m {s}s"
                        except Exception:
                            pass

                    print("  Live stats  :")
                    print(f"    CPU       : {stat['cpu']}")
                    print(f"    RAM       : {mem_used_part} ({mem_used_bytes} bytes)")
                    print(f"    Net I/O   : {stat['netio']}")
                    print(f"    Block I/O : {stat['blockio']}")
                    print(f"    PIDs      : {stat['pids']}")
                    print(f"    Uptime    : {uptime_str}")
                else:
                    print("  Live stats  : (not available)")

        print("\n" + "-"*80)
        print(f"Running storages : {running_count}/{len(storages)}")
        print(f"Total RAM (running): {self._format_bytes(total_mem_bytes)} ({total_mem_bytes} bytes)")
        print(f"Total overlay disk: {self._format_bytes(total_disk_bytes)} ({total_disk_bytes} bytes)")
        print("-" * 80)
        print("\n")


def print_usage():
    print("""
================================================================
     VNC Desktop Storage Manager
================================================================

USAGE:
  python main.py create <name>          Create storage
  python main.py start <name> [port]    Start VNC (default 2000)
  python main.py start <name> terminal  Terminal mode (persistent)
  python main.py stop <name>            Stop
  python main.py rename <old>:<new>     Rename storage
  python main.py delete <name>          Delete storage (force if running)
  python main.py list                   Detailed status and metrics

EXAMPLES:
  python main.py create dev
  python main.py start dev 2000
  python main.py stop dev
  python main.py delete dev
""")


def main():
    if len(sys.argv) < 2:
        print_usage()
        sys.exit(1)

    command = sys.argv[1].lower()
    manager = StorageManager()

    if command == "create":
        if len(sys.argv) < 3:
            print("Usage: python main.py create <name>")
            sys.exit(1)
        manager.create(sys.argv[2])

    elif command == "start":
        if len(sys.argv) < 3:
            print("Usage: python main.py start <name> [port|terminal]")
            sys.exit(1)
        name = sys.argv[2]
        port_or_mode = sys.argv[3] if len(sys.argv) > 3 else "2000"
        manager.start(name, port_or_mode)

    elif command == "stop":
        if len(sys.argv) < 3:
            print("Usage: python main.py stop <name>")
            sys.exit(1)
        manager.stop(sys.argv[2])

    elif command == "rename":
        if len(sys.argv) < 3:
            print("Usage: python main.py rename oldname:newname")
            sys.exit(1)
        manager.rename(sys.argv[2])

    elif command == "delete":
        if len(sys.argv) < 3:
            print("Usage: python main.py delete <name>")
            sys.exit(1)
        manager.delete(sys.argv[2])

    elif command == "list":
        manager.list_storages()

    elif command == "help":
        print_usage()

    else:
        print(f"Unknown command: {command}")
        print_usage()
        sys.exit(1)


if __name__ == "__main__":
    main()
