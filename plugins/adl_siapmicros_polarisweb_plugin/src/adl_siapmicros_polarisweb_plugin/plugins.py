import logging
from datetime import timedelta

from adl.core.registries import Plugin

logger = logging.getLogger(__name__)


class PolarisWebPlugin(Plugin):
    type = "adl_siapmicros_polarisweb_plugin"
    label = "ADL SIAPMicros PolarisWeb Plugin"

    def get_default_start_date(self, station_link):
        end_date = self.get_default_end_date(station_link)
        return end_date - timedelta(days=1)

    def get_start_date_from_db(self, station_link):
        start_date = super().get_start_date_from_db(station_link)

        if start_date:
            # Advance by 1 minute to avoid re-fetching already stored data
            start_date += timedelta(minutes=1)

        return start_date

    def get_station_data(self, station_link, start_date=None, end_date=None):
        client = station_link.network_connection.get_api_client()

        # Collect all Polaris measure IDs configured for this station link
        measure_ids = [
            mapping.polaris_measure_id
            for mapping in station_link.get_variable_mappings()
        ]

        if not measure_ids:
            logger.warning(
                "No variable mappings configured for station link %s — skipping.",
                station_link,
            )
            return []

        records, sources_count = client.get_measurements(
            station_id=station_link.polaris_station_id,
            measure_ids=measure_ids,
            start_date=start_date,
            end_date=end_date,
        )

        # Duck-typed sources-count handover: core stores this on the run's
        # activity log so "looked, found nothing" (0) stays distinguishable
        # from "never looked" (None). Committed only here, once the response
        # is parsed — a call that raised, or the early return above, leaves the
        # attribute None, and core's evidence rule abstains on NULL rather
        # than blaming the source for a run that never asked it anything.
        if getattr(station_link, "adl_sources_count", None) is None:
            station_link.adl_sources_count = 0
        station_link.adl_sources_count += sources_count

        return records
