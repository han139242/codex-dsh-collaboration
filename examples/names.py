def unique_names(names):
    result = []
    seen = set()
    for name in names:
        spelling = name.strip()
        if not spelling:
            continue
        key = spelling.casefold()
        if key not in seen:
            seen.add(key)
            result.append(spelling)
    return result
