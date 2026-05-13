RAW_MESSAGE_TARGETS = set()


def register_raw_message_target(name):
    if name:
        RAW_MESSAGE_TARGETS.add(name)


def get_raw_message_targets():
    return sorted(RAW_MESSAGE_TARGETS)
