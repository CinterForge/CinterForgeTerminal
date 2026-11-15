"""CinterForge runtime modules owned by the single staged Python HTTP service.

PURPOSE: Group small, explicit internal owners without starting another server.
CONTENTS: CinterForgeLogging owns the persistent Server Log; CinterForgeCode
owns Code-OSS workspace selection and validated file operations;
CinterForgeGuest owns the existing server's boot scheduling, Bash/VNC
readiness publication and connection maintenance. CinterForgeVM owns the
existing runtime t-profile state root, mount configuration, startup and Docker
readiness decisions, not worker lifetime or guest shells.
IMPORTS: The service imports these from its *staged* sibling webapp folder;
the native Host still owns exactly one worker and HTTP listener.
STARTUP: No side effects or hidden dependency/VM provisioning in this module.
SHUTDOWN: Each owner is released by the existing Python server lifecycle.
SECURITY: Authorization stays in the primary service and filesystem checks
stay in their respective modules; no new browser routes are created here.
"""

# Lifecycle Setup/Build/Test owns its separate developer VM and guest acceptance
# implementations under host/lifecycle. Never import host or a lifecycle
# module from this installed application runtime package.
