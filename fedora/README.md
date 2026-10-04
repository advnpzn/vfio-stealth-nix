# Fedora port

Native Fedora 44 / x86_64 builds of this repository's virtualization stack,
without installing Nix. Build outputs are staged and packaged into a separate
`vfio-stealth-amd` or `vfio-stealth-intel` RPM. The original NixOS entry points
remain available.

## Sources and versions

`sources.lock.json` records the package versions resolved from the repository's
locked nixpkgs commit `7a0f122f5090cf4c2ade2a13a0e229d4e19ba71f`:

| Component | AMD | Intel | Selection |
|---|---|---|---|
| QEMU | 11.1.0 | 11.0.3 | Same floor/ceiling/patch-series rules as `lib/select-qemu-base.nix` |
| EDK2/OVMF | 202608 | 202608 | EDK2 at the locked nixpkgs revision |
| AutoVirt EDK2 patch | stable202605 | stable202605 | Latest vendored patch per vendor, as in `ovmf/package.nix` |
| Linux | 7.2.8 | No kernel patches | Locked nixpkgs `linux_latest`, one of the repository's two kernel test paths |

The base nixpkgs QEMU is 11.1.1. It exceeds both vendored patch ceilings, so
the resolver selects 11.1.0 for AMD and 11.0.3 for Intel. The minimum 11.0.3
alone is **not** the repository's current AMD selection. Fedora's installed
QEMU version has no influence on this decision.

Source metadata was read from these files **at that commit**, not from current
upstream branches:

- `pkgs/by-name/qe/qemu/package.nix`
- `pkgs/by-name/ed/edk2/package.nix`
- `pkgs/applications/virtualization/OVMF/default.nix`
- `pkgs/os-specific/linux/kernel/kernels-org.json`

QEMU archives use the SHA-256 values from this repository's `qemu/package.nix`.
The Linux archive uses the decoded nixpkgs checksum. EDK2 is checked out at
immutable commit `2970e5699ba6267f3384ffab20f96647578aebc8`, including its pinned
submodules; the Nix source hash in the lock is provenance, not a tarball hash.
No failed download or patch triggers a fallback to another version.

The QEMU, OVMF and kernel post-patch scripts are extracted from the existing
`.nix` shell literals with a deliberately restricted renderer. Unknown Nix
expressions fail. GNU patch still runs with zero fuzz, and all the upstream
post-patch assertions still execute. A changed flake revision or source-selection
input requires a reviewed lock update.

## Build

Run from the repository root. Use a path without spaces. Keep your local
configuration under the ignored `build/` directory:

```sh
mkdir -p build/fedora
cp fedora/config.example.json build/fedora/config.json
python3 fedora/build.py plan --config build/fedora/config.json
```

Edit the configuration before generating domain XML. `CHANGE-ME` values are
placeholders, not detected hardware identities. `qemu` accepts the literal
build options from `qemu/package.nix`; omitted options retain those defaults.
Build-time values have a restricted character set because the existing patch
scripts interpolate them into shell, sed and C source. Unknown options are
errors. For Intel set `cpuVendor` to `intel` and all three `kernel` options to
`false`.

The builder container installs compilation dependencies without changing the
host's package set:

```sh
podman build -f fedora/Containerfile -t localhost/vfio-stealth-builder:44 .

podman run --rm --userns=keep-id \
  -v "$PWD:/work:ro,z" \
  -v "$PWD/build/fedora:/work/build/fedora:rw,z" \
  localhost/vfio-stealth-builder:44 \
  build --config build/fedora/config.json --jobs 8

podman run --rm --userns=keep-id --entrypoint python3 \
  -v "$PWD:/work:ro,z" \
  -v "$PWD/build/fedora:/work/build/fedora:rw,z" \
  localhost/vfio-stealth-builder:44 fedora/smoke.py --vendor amd

podman run --rm --userns=keep-id \
  -v "$PWD:/work:ro,z" \
  -v "$PWD/build/fedora:/work/build/fedora:rw,z" \
  localhost/vfio-stealth-builder:44 \
  rpm --config build/fedora/config.json
```

The container uses Fedora build dependencies, not Fedora's QEMU/OVMF sources.
The Fedora base image and toolchain are not bit-for-bit pinned; this preserves
the repository's component versions, not Nix's entire derivation closure.
On a native Fedora build host, install the dependencies listed in `Containerfile`
and run the same Python commands directly.

Useful stages:

```sh
python3 fedora/build.py fetch --component qemu --config build/fedora/config.json
python3 fedora/build.py prepare --component qemu --config build/fedora/config.json
python3 fedora/build.py patch-script --component kernel --config build/fedora/config.json
python3 fedora/build.py check-kernel --config build/fedora/config.json --jobs 8
python3 -m unittest discover -s fedora -p 'test_*.py' -v
```

`fetch` verifies cached archives too. `prepare` applies patches transactionally.
`check-kernel` compiles the selected patched KVM code with x86_64 defconfig,
AMD KVM and Hyper-V enabled, and BTF debug information disabled. It does not
build an installable kernel or replace a host-configured kernel build.
If the config changes after preparation, use a fresh `--work` directory;
do not reuse partially patched sources. `build` stages everything under
`build/fedora/stage`, and `rpm` writes `build/fedora/rpmbuild/RPMS/x86_64/`.
The RPM packages those local binaries; it is not a rebuildable source RPM.
No command installs packages, updates a bootloader, or modifies a live VM.

OVMF is built for X64 with Secure Boot, SMM, TPM/TPM2 and a 4 MiB flash layout.
`virt-fw-vars --enroll-microsoft` enrolls Microsoft OEM keys into the newly built variable store and
creates `OVMF_VARS.ms.fd`. This uses that tool's Microsoft OEM certificate set;
the Nix build instead uses Debian's enrollment tooling/certificate. Neither
approach copies or overwrites an existing VM's mutable NVRAM.

## Kernel

The optional kernel build uses Linux **7.2.8**, not whatever Fedora happens to
ship next. The upstream-kernel path is selected; this is not a CachyOS build.
Supply a Fedora kernel configuration appropriate to your host:

```sh
cp /boot/config-7.2.8-200.fc44.x86_64 build/fedora/kernel.config
# Review signing/certificate paths in this copied config for your build environment.
python3 fedora/build.py kernel --config build/fedora/config.json \
  --kernel-config build/fedora/kernel.config --jobs 8
```

This produces RPMs with `LOCALVERSION=-vfio-stealth` under
`build/fedora/kernel-rpmbuild/`. The container can run this command too, using
the same mounts as above. It retains the supplied kernel signing configuration;
Fedora's private signing keys are not available to a local build. Use your own
signing setup if host Secure Boot is enabled. The build does not disable signing
or enroll a key automatically.

Timing and CPUID patches require AMD. CPUID passthrough takes precedence over
CPUID spoofing, requires `hypervMode: "hidden"`, and requires every host thread
to be present as a guest vCPU. BetterTiming changes KVM behavior for **all** VMs
on the host, as documented by the original module.

For the default AMD configuration, the original module requests:

```text
processor.max_cstate=1 kvm_amd.vls=0 kvm_amd.vgif=0
kvm.ignore_msrs=1 kvm.report_ignored_msrs=0 tsc=reliable
```

Apply any chosen arguments only to the new kernel's entry with `grubby`; retain
the stock kernel for rollback. The port does not edit boot entries or VFIO PCI
bindings. Firmware IOMMU setup and assigning a GPU remain host-specific.

## Install and convert a VM

After inspecting and testing your build, install the generated RPM with DNF.
Its emulator is `/usr/libexec/vfio-stealth/amd/bin/qemu-system-x86_64` (substitute
`intel` as needed); Fedora's emulator remains separately installed.

Keep SELinux enforcing. Custom emulator/data paths need suitable labels for
libvirt's sVirt domain. For an AMD install:

```sh
sudo semanage fcontext -a -t qemu_exec_t '/usr/libexec/vfio-stealth/amd/bin/qemu-system-x86_64'
sudo semanage fcontext -a -t virt_content_t '/usr/share/vfio-stealth/amd(/.*)?'
sudo semanage fcontext -a -t virt_content_t '/usr/share/edk2/vfio-stealth-amd(/.*)?'
sudo restorecon -Rv /usr/libexec/vfio-stealth/amd /usr/share/vfio-stealth/amd /usr/share/edk2/vfio-stealth-amd
```

`semanage` is supplied by `policycoreutils-python-utils`. If a local rule already
exists, inspect it and use `-m` to change that exact rule. Check audit logs if
your local policy needs additional access; do not turn off SELinux to hide an
access failure.

Export an existing **x86_64 Q35** domain, convert it to a separate file, and
validate it before defining it:

```sh
virsh -c qemu:///system dumpxml --inactive win11 > build/fedora/win11.original.xml
python3 fedora/domain.py build/fedora/win11.original.xml build/fedora/win11.fedora.xml \
  --config build/fedora/config.json \
  --nvram /var/lib/libvirt/qemu/nvram/win11_stealth_VARS.fd
virt-xml-validate build/fedora/win11.fedora.xml domain
```

The converter preserves the name, UUID, disks, CPU topology, PCI passthrough
devices, network backends and existing TPM state. It sets the custom emulator,
firmware, SMBIOS/ACPI inputs, clocks and CPU identity. It uses QOM CPU globals
instead of adding a second `-cpu` that would overwrite libvirt's Hyper-V flags.
It removes the configured VirtIO balloon/RNG/input devices and panic device.
VirtIO disks and network cards are preserved; replacing their models requires
guest-driver preparation. MAC rewriting changes only the prefix and can be
disabled with an empty `macPrefix`.

The converter refuses to reuse the old NVRAM path, overwrite its output, convert
a different chipset, or silently merge conflicting raw QEMU arguments. Existing
NVRAM enrollment/custom firmware settings are not migrated into the new store.
Requested kernel-dependent Hyper-V features require `--kernel-config` proving
`CONFIG_KVM_HYPERV=y`. CPUID passthrough also requires `--host-threads N`.

When the VM is shut down and the XML has been reviewed, use
`virsh -c qemu:///system define build/fedora/win11.fedora.xml` to activate it.
The original XML and original NVRAM remain your rollback path. Reverting is a
separate `virsh define` of the original XML while the VM is off.

## Validation and scope

Offline tests cover version selection, generated shell syntax, option rejection,
domain preservation, libvirt schema validation and Hyper-V capability gates.
`prepare` adds real-source patch validation. A successful compile/package is
not proof of VM boot or of any detection result: run a boot test using the built
QEMU/OVMF, then `guest/verify-host.sh` on the host and the supplied PowerShell
verification script in Windows.

The native port includes QEMU/OVMF patching, the three ACPI tables, SMBIOS binary
tables, the existing guest tools and AMD kernel patch selection. Optional
`libtpms` identity patching and rewriting the host's `swtpm-localca.options`
are not implemented; existing Fedora TPM services are preserved. Nixpkgs-only
build adaptations (including its OpenSSL de-vendoring) are not reproduced.
The source versions and this repository's stealth patches are preserved;
the resulting binaries are not claimed to be identical to Nix outputs.

`fedora/smoke.py` boots the built EFI shell from USB test media under TCG with a disposable
unenrolled variable store, requires a marker from `startup.nsh`, and checks
that the guest shuts down. Use `python3 fedora/smoke.py --accel kvm` to repeat
the test with host KVM access. Neither mode tests Windows, GPU passthrough,
or enforcement of the production Secure Boot key set.

### Validation performed on 2026-10-04

For the AMD example configuration in the Fedora 44 builder:

- QEMU 11.1.0 and Linux 7.2.8 archive checksums matched the repository-derived lock.
- QEMU, EDK2 and the default timing + CPUID-spoof kernel patches applied successfully.
- QEMU 11.1.0 and EDK2 202608 compiled; the firmware enrollment tool produced
  `OVMF_VARS.ms.fd` with Microsoft OEM keys.
- All three ACPI tables compiled, and all seven generated SMBIOS tables verified.
- The patched Linux 7.2.8 KVM subsystem compiled, including `svm.o` and `kvm-amd.o`.
- The EFI shell boot test passed under TCG and shut down successfully.
- The same EFI shell test passed with host KVM on Fedora's stock AMD kernel.
- Direct Linux boot (`-kernel`, default BIOS) with the patched QEMU timed out
  when testing Fedora's stock kernel. Use the tested OVMF/UEFI path for this
  stack. The independent kernel boot test uses Fedora's QEMU.
- Nine offline tests passed, including validation against libvirt's domain XML schema.
- Ruff checks and formatting checks passed.
- `vfio-stealth-amd-11.1.0-1.fc44.x86_64.rpm` was built (about 26 MiB).

The RPM uses example build-time identities. Customize and rebuild before using
your own hardware identity. A subsequent host installation installed this RPM
alongside Fedora's QEMU and updated an existing VM to use it and the patched
OVMF, preserving its TPM and UEFI variables and using a new disk overlay.
Libvirt accepted the updated domain. Intel has resolver/rendering coverage but
was not compiled in this validation run. A full host kernel RPM, a Windows guest
boot, GPU passthrough, and the optional TPM identity changes remain unvalidated.
