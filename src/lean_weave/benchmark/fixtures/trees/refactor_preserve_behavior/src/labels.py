def classify(value):
    text = value.strip().lower()
    if text in {"yes", "y"}:
        return "truthy"
    return "other"
