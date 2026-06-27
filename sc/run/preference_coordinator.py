"""Back-compat shim. PreferenceCoordinator moved to sc/preference_coordinator.py
so it can be vendored into the plugin (it's pure — features/policy/preferences
only, no run/ deps). Importers here continue to work unchanged."""

from ..preference_coordinator import PreferenceCoordinator, PreferenceMatch  # noqa: F401
