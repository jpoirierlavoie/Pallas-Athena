"""Pagination helper for list views."""

PAGE_SIZE = 15


def paginate(items: list, page: int, page_size: int = PAGE_SIZE) -> tuple[list, dict]:
    """Slice a list for the current page and return pagination context.

    Returns (page_items, pagination_dict).
    """
    page = max(1, page)
    offset = (page - 1) * page_size
    page_items = items[offset:offset + page_size]
    return page_items, {
        "page": page,
        "has_prev": page > 1,
        "has_next": len(items) > offset + page_size,
    }
