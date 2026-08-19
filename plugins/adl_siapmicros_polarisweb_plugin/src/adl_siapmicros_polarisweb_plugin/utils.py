def get_stations(network_conn):
    client = network_conn.get_api_client()
    stations_dict = client.get_stations()

    stations_list = []
    for station_id, station in stations_dict.items():
        name = station.get("name", "")
        label = f"{name} (ID: {station_id})"
        stations_list.append({"label": label, "value": station_id})

    return sorted(stations_list, key=lambda s: s["label"])


def get_measures(network_conn):
    client = network_conn.get_api_client()
    measures_dict = client.get_base_measures()

    measures_list = []
    for measure_id, measure in measures_dict.items():
        name = measure.get("name", "")
        short_name = measure.get("short_name", "")

        # hide Rain_Tot as it might be confusing with Rain_Prev.
        # We only want to show measures that represent a single point in
        # time, not all accumulated values.
        if short_name == "Rain_Tot":
            continue

        unit = measure.get("unit", "")
        label = f"{name} - {short_name} ({unit})" if short_name else f"{name} ({unit})"
        measures_list.append({"label": label, "value": measure_id})

    return sorted(measures_list, key=lambda m: m["label"])
