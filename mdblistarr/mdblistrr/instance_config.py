"""Small shared primitives for equivalent Radarr and Sonarr instance settings."""

ROLE_LABELS = {
    "is_library_source": "Permanent library source",
    "is_ondemand_target": "On-Demand target",
    "enable_queue_import": "Enable MDBList queue import",
}

ROLE_HELP_TEXT = {
    "is_library_source": "Permanent library source: read-only for library state and comparison.",
    "is_ondemand_target": "On-Demand target: controlled reconciliation writes may only be made where supported.",
    "enable_queue_import": "Queue import capability: explicit opt-in and requires quality profile/root folder.",
}


def queue_import_value_is_valid(value):
    """Return whether a profile/root value is safe to use for queue import."""
    return value is not None and str(value).strip() not in ("", "0")


def queue_import_requirements_are_valid(quality_profile, root_folder):
    return queue_import_value_is_valid(quality_profile) and queue_import_value_is_valid(root_folder)
