"""Query-string links preserve monitoring filters without adding filter logic."""
SORTS = {"default", "collector", "active", "activity", "overdue"}


def navigation(params):
    """Return canonical sorting and filter links, resetting pagination on changes."""
    params = params.copy()
    sort, direction = params.get("sort", "default"), params.get("dir", "asc")
    if sort not in SORTS or direction not in {"asc", "desc"}:
        sort, direction = "default", "asc"
    params["sort"], params["dir"] = sort, direction
    params.pop("page", None)

    def link(**changes):
        updated = params.copy()
        for key, value in changes.items():
            if value is None:
                updated.pop(key, None)
            else:
                updated[key] = value
        return "?" + updated.urlencode()

    cards = {key: link(availability=key) for key in ("online", "busy", "offline")}
    cards.update(approved=link(availability=None, overdue=None), overdue=link(overdue="1"))
    headers = {key: link(sort=key, dir="desc" if sort == key and direction == "asc" else "asc")
               for key in SORTS - {"default"}}
    return sort, direction, cards, headers, params.urlencode()
