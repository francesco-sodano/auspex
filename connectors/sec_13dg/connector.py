from shared.sec_efts_connector import SecEftsConnector


class Sec13DgConnector(SecEftsConnector):
    source_id = "sec_13dg"
    schema_version = 2
    forms = (
        "SC 13D,SC 13D/A,SC 13G,SC 13G/A,"
        "SCHEDULE 13D,SCHEDULE 13D/A,SCHEDULE 13G,SCHEDULE 13G/A"
    )
    archive_profile = "13dg"
    window_days = 7
    require_exhaustive_efts = True
