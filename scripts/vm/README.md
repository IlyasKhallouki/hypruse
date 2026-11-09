# Omarchy test VM

These scripts build a clean Omarchy 4.5 install in a QEMU VM and run the hypruse quick start in it: `yay -S hypruse`, `hypruse init`, `hypruse doctor`, then the calls an agent makes first. Use it to check the README against a stock Omarchy desktop instead of a desktop you have configured yourself.

Hyprland needs a GPU render node, so a container will not do. The VM gets a virtio GPU, and the guest gets a `/dev/dri/renderD128` from it, as in Omarchy's own install tests.

## Status

Nobody has run this end to end yet. On the development host (2026-11-09), `create`, `snapshot` and `reset` worked on a 64 MiB disk in /dev/shm. QEMU 11.1.2, with the display modules loaded from a scratch directory, accepted every flag `boot` builds in all four display modes (the window ones through SDL's offscreen driver), and the UEFI firmware drew through `virtio-vga-gl` on `egl-headless`. `check` reached every step against a stand-in for ssh, and each guest command passed `bash -n`. The real download, install and check need the disk space below.

## What you need

On the host, an x86_64 Arch system with:

- `qemu-system-x86`, `qemu-img` and `edk2-ovmf`.
- The QEMU display modules, which Arch packages on their own. `boot` names the ones that are missing. For the default window and headless modes:

  ```sh
  sudo pacman -S --needed qemu-hw-display-virtio-gpu qemu-hw-display-virtio-gpu-gl \
    qemu-hw-display-virtio-vga qemu-hw-display-virtio-vga-gl \
    qemu-ui-opengl qemu-ui-sdl qemu-ui-egl-headless
  ```

  QEMU only loads modules from its own build. If the repositories have a newer QEMU than the one you run, upgrade with `pacman -Syu` and these packages in the same command.
- Read and write access to `/dev/kvm` (the `kvm` group).
- `curl`, `gpg`, `python3` and `openssh`, which most Arch systems already have.
- Free RAM for the guest: 8 GiB by default, the same as Omarchy's own test VM. `--mem 6G` also works.

## Disk space

| What | Size |
|---|---|
| The ISO, `omarchy-4.5.0-x86.iso` | 6,540,910,592 bytes (6.1 GiB) |
| The VM disk, a thin qcow2 with a 40 GiB virtual size | about 200 KiB empty, an estimated 10 to 20 GiB after the install (not measured yet) |
| Snapshots | only the blocks that change after each one |

Plan for 30 GiB free in the VM directory, `~/.cache/hypruse-vm` unless you set `HYPRUSE_VM_DIR`. `scripts/vm/fetch --check` compares the ISO's size with the free space and downloads nothing. You only need the ISO for the install, so you can delete it once the clean snapshot exists. `fetch` downloads it again if you need it.

## The scripts

| Script | What it does |
|---|---|
| `fetch` | downloads the ISO and checks its SHA-256 and GPG signature |
| `create` | makes the empty disk and the UEFI variable store |
| `boot` | starts the VM, in a window or headless |
| `shell` | ssh into the guest; `--bootstrap` sets up ssh on a fresh install |
| `snapshot` | takes, lists or deletes snapshots of the stopped VM |
| `reset` | returns the stopped VM to a snapshot |
| `stop` | shuts the VM down |
| `type` | types text into the VM, for the passphrase when there is no window |
| `check` | runs the quick start in the guest and prints a pass/fail summary |

Each one prints its options with `--help`.

## 1. Download the ISO

```sh
scripts/vm/fetch
```

It downloads `https://iso.omarchy.org/omarchy-4.5.0-x86.iso`, resuming if interrupted, then checks two things before it renames the file into place:

- the SHA-256 that omarchy.org publishes, `5d6288bc57f96f6231719830457582c846a2291985201c36a9a7d1216978b00f`
- the detached signature `omarchy-4.5.0-x86.iso.sig`, made by `Omarchy <pkgs@omarchy.org>`. The key's fingerprint is pinned as `40DF B630 FF42 BCFF B047 046C F013 4EE6 80CA C571`. The key itself is fetched from `builder/omarchy.gpg` in the omacom/omarchy-iso repository and refused if its fingerprint differs.

For a later release, set `OMARCHY_VERSION` and `OMARCHY_ISO_SHA256` (the SHA-256 button under the download on omarchy.org).

## 2. Create the disk

```sh
scripts/vm/create
```

This makes a 40 GiB thin qcow2, the size Omarchy's own test VM uses, and a fresh UEFI variable store. Pass `--size` to change it.

## 3. Install Omarchy

```sh
scripts/vm/boot --iso
```

A QEMU window opens. The empty disk has nothing to boot, so the firmware moves on to the ISO, which starts the installer with no boot menu. The installer is a full-screen menu driven by the keyboard. These are the screens Omarchy 4.5.0 shows, in order:

1. On the language screen, pick English.
2. On the keyboard screen ("Let's setup your machine..."), pick English (US). Do not press Ctrl+C here: that prepares the machine for another owner and skips creating your user.
3. The account screen ("Let's setup your user account...") asks for the timezone, a username, a password and a hostname. Use the username `tester`, which the scripts expect, or set `HYPRUSE_VM_USER` to the name you chose. The password becomes the sudo password and, on an encrypted install, the disk passphrase.
4. At "Does this look right?", choose Yes.
5. At "Select install disk", pick the 40 GiB virtio disk, `/dev/vda`. The installer leaves the ISO out of the list.
6. The next screen warns "Everything will be overwritten. There is no recovery possible." Choose "Yes, install". If a mode menu comes up first, choose "Full disk install". Encryption is on by default, and you should keep it: an encrypted install logs straight into Hyprland once you type the passphrase, while an unencrypted one (Ctrl+C on this screen switches to it) stops at a login screen. The checks work either way.
7. The installer checks the install medium, then runs for a few minutes. When it shows "Installed Omarchy", choose "Reboot Now".

The VM boots from the disk this time, because the disk comes first in the boot order.

## 4. Turn on ssh

A stock Omarchy install ships with sshd disabled and the firewall closed. To open it to the host only:

1. Type the disk passphrase in the VM window. Omarchy logs you in and starts Hyprland.
2. On the host, run:

   ```sh
   scripts/vm/shell --bootstrap
   ```

   It serves a short setup script on 127.0.0.1 and prints one line to type in the guest.
3. In the guest, open a terminal with Super + Enter and type that line:

   ```sh
   curl -fsS http://10.0.2.2:8022/bootstrap | bash
   ```

   The guest reaches the host as 10.0.2.2. The script adds the VM's ssh key (`~/.cache/hypruse-vm/id_ed25519`) to `~/.ssh/authorized_keys`, allows port 22 from 10.0.2.2 in ufw and enables sshd. sudo asks for your password once.
4. The host side notices when ssh works and stops serving. From now on, `scripts/vm/shell` opens a shell in the guest and `scripts/vm/shell CMD` runs one command.

## 5. Take the clean snapshot

```sh
scripts/vm/stop
scripts/vm/snapshot clean
```

`stop` presses the virtual power button and waits for the guest to power off. The `clean` snapshot holds a fresh install whose only changes are the three ssh ones above. It also keeps a copy of the UEFI variable store, which lives outside the qcow2 and holds the boot entry the installer wrote.

## 6. Run the check

```sh
scripts/vm/boot
```

Type the disk passphrase, wait for the desktop, then on the host:

```sh
scripts/vm/check
```

It asks for the guest password, which `yay` needs for sudo. To skip the prompt, set `HYPRUSE_VM_PASSWORD`. The password goes to the guest over ssh into a file under `/run/user` that only the guest user can read, and `check` removes it at the end.

Every step runs over ssh inside the guest's Hyprland session, with the session's variables set as they are in a terminal there:

| Step | What runs | It passes when |
|---|---|---|
| `ssh` | `true` | the guest answers on 127.0.0.1:2222 |
| `session` | `hyprctl -j version` | a Hyprland instance and a Wayland display are up |
| `system` | `omarchy-version`, `pacman -Q` | always; it records the versions |
| `yay` | `command -v yay` | yay is installed, as the README assumes |
| `install` | `yay -S --noconfirm --needed hypruse` | the AUR package builds and installs |
| `version` | `hypruse --version` | a working `hypruse` is on PATH |
| `init` | `hypruse init --yes` | it registers with the clients it finds and reaches its doctor run |
| `register` | `claude mcp get hypruse` | Claude Code runs the installed `hypruse`, not `uvx`, and reports it connected |
| `doctor` | `hypruse doctor` | every doctor line is `[ok]` |
| `desktop` | `hypruse desktop` | the shell verb lists the desktop |
| `mcp` | a small MCP client over stdio | the server starts, lists its tools and answers `desktop` |
| `agent` | `claude -p "Use hypruse to list the windows on each of my workspaces."` | only with `--agent`: the answer mentions a workspace |
| `screen` | `grim` | a screenshot of the guest comes back as a PNG |

`init --yes` answers the questions that `hypruse init` asks in the README. The `mcp` step makes the same first call an agent would and needs no agent account. The `agent` step asks the README's own question, so it needs a Claude Code login in the guest. Log in once with `scripts/vm/shell` and `claude`, then run `scripts/vm/check --agent`. If you take a snapshot after logging in, that snapshot holds your Claude credentials.

`check` prints a line per step as it goes, then the summary, and exits 1 if any step failed. The full output of every command and the screenshot go to `~/.cache/hypruse-vm/checks/<date-time>/` (`check.log`, `screen.png`).

To try a change before releasing it, `scripts/vm/check --source share` installs the checkout from the shared folder (below) with `uv tool install` instead of the AUR. `--source skip` keeps whatever hypruse the guest already has.

## 7. Back to clean

```sh
scripts/vm/stop
scripts/vm/reset
```

`reset` with no name goes back to `clean`, disk and UEFI variables both. Boot again and run the next check.

## Headless

```sh
scripts/vm/boot --headless --detach
```

There is no window. The screen is on VNC at 127.0.0.1:5905, for any VNC viewer (`vncviewer 127.0.0.1:5905` from `tigervnc`). The display still has GL, rendered on the host's GPU through `egl-headless`. Without a viewer, type the disk passphrase blind once the VM has had time to reach the prompt:

```sh
scripts/vm/type 'your passphrase'
```

`type` sends keystrokes through QEMU's monitor and presses Enter. It assumes a US layout in the guest. `scripts/vm/type --key esc` presses a single key. The install itself is easier with a window or a viewer, since its menus have to be read.

## The shared checkout

`boot` shares the checkout it runs from with the guest, read-only, under the 9p tag `hypruse`. To mount it in the guest:

```sh
sudo mkdir -p /mnt/hypruse
sudo mount -t 9p -o trans=virtio,version=9p2000.L,ro hypruse /mnt/hypruse
```

QEMU enforces the read-only part, so nothing in the guest can change the checkout. `--share DIR` shares another directory and `--no-share` shares none.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `HYPRUSE_VM_DIR` | `~/.cache/hypruse-vm` | ISO, disk, UEFI vars, ssh key, logs |
| `HYPRUSE_VM_USER` | `tester` | the guest user `shell` and `check` log in as |
| `HYPRUSE_VM_PASSWORD` | none | the guest user's password, for sudo in `check` |
| `HYPRUSE_VM_SSH_PORT` | `2222` | host port forwarded to the guest's port 22 |
| `OMARCHY_VERSION` | `4.5.0` | the release `fetch` downloads |
| `OMARCHY_ISO_SHA256`, `OMARCHY_ISO_URL` | pinned for 4.5.0 | for a release the scripts do not know yet |
| `HYPRUSE_VM_RENDERNODE` | the first `/dev/dri/renderD*` | the host GPU node for GL |

`boot` also takes `--mem`, `--cpus` (default: the host's threads, at most 8), `--vnc N`, `--paused`, `--timeout SECS`, `--print` (show the QEMU command line) and, after `--`, any extra QEMU arguments.

## Troubleshooting

| What you see | What to do |
|---|---|
| `boot` says modules are missing | Install the packages it names. They must come from the same QEMU build as `qemu-system-x86`, so if the repositories have moved on, upgrade with `pacman -Syu` and the packages together. |
| A black screen, or GL errors from QEMU | Boot with `--no-gl`. The guest then renders in software on plain `virtio-vga`, which is how Omarchy's own headless install test runs. |
| `check` says no ssh after a reset | You took the snapshot before step 4. Run `scripts/vm/shell --bootstrap` again and take a new snapshot. |
| The disk file keeps growing | Run `sudo fstrim -av` in the guest. `boot` attaches the disk with discard on, so freed blocks go back to the qcow2. Snapshots keep the blocks they need, and `scripts/vm/snapshot --delete NAME` frees them. |
| The 9p mount fails | The guest kernel may lack 9p support. Use `--source aur`, or copy files in with `scripts/vm/shell 'cat > file' < file`. |

## Unattended reinstalls

The Omarchy ISO skips its wizard when it finds a second drive labeled `cidata` carrying `user_configuration.json` and `user_credentials.json`. With an `authorized_keys` file there too, it also enables sshd and opens the firewall for it. The files are the ones the wizard writes, so one interactive install gives you a set. The [manual](https://omarchy.org/manual/unattended-installs/) shows how to build the drive. To attach it, run `scripts/vm/boot --iso -- -drive file=cidata.iso,media=cdrom,readonly=on`. This path has not been tried with these scripts.

## Sources

- The ISO URL and SHA-256: the download section on [omarchy.org](https://omarchy.org), for the [v4.5.0 release](https://github.com/omacom/omarchy/releases/tag/v4.5.0) of 2026-11-05 from the `quattro` branch.
- The signing key: `builder/omarchy.gpg` in [omacom/omarchy-iso](https://github.com/omacom/omarchy-iso).
- The machine, firmware, GPU and network flags, the 40 GiB disk and the ssh bootstrap follow `bin/omarchy-iso-boot` and `bin/omarchy-iso-test` in the same repository.
- The installer screens: `configs/airootfs/root/configurator` and its English strings there.
