# Import into a NixOS host configuration when its root-owned user.slice
# pressure monitor would ignore Fleet's per-CI-unit OOM preference.
{ lib, ... }:
{
  # Keep userspace memory-pressure protection, but select victims separately
  # within each user's delegated hierarchy. A root-owned cross-user monitor
  # ignores user-owned cgroup preferences.
  systemd.slices.user.sliceConfig.ManagedOOMMemoryPressure = lib.mkForce "auto";
  systemd.services."user@" = {
    overrideStrategy = "asDropin";
    # Resource-policy reload must not restart users' service managers.
    restartIfChanged = false;
    serviceConfig = {
      ManagedOOMMemoryPressure = "kill";
      ManagedOOMMemoryPressureLimit = "80%";
    };
  };
}
