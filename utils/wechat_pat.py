"""Read WeChat appmsg type 62 without mistaking a suffix for the pat target."""
import xml.etree.ElementTree as ET


def pat_event(context):
    if not isinstance(context, dict):
        return None
    layers, current = [], context
    for _ in range(4):
        if not isinstance(current, dict):
            break
        layers.append(current)
        current = current.get('raw') or current.get('_raw')
    # Functional XML has precedence over abbreviated display text.
    for layer in layers:
        raw_type = layer.get('localType', layer.get('local_type'))
        if raw_type is not None:
            try:
                if int(raw_type) & 0xffffffff not in {49, 10000, 10002}:
                    continue
            except (TypeError, ValueError):
                continue
        for field in ('rawContent', 'raw_content', 'content'):
            text = str(layer.get(field) or '')
            if len(text) > 65536 or '<!DOCTYPE' in text.upper() or '<!ENTITY' in text.upper():
                continue
            start = text.find('<')
            if start < 0:
                continue
            try:
                root = ET.fromstring(text[start:])
            except ET.ParseError:
                continue
            app = root if root.tag == 'appmsg' else root.find('appmsg')
            if app is None or (app.findtext('type') != '62' and app.find('patinfo') is None):
                continue
            return dict(target=(app.findtext('patinfo/pattedusername') or '').strip(),
                        actor=(app.findtext('patinfo/fromusername') or '').strip(),
                        title=(app.findtext('title') or '').strip())
    return None
