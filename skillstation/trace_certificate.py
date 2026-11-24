"""Current completion certificate based on action presence."""
from .operation_capture import actions

def issue(records):
    present = actions(records)
    return {'status': '完整' if {'deenergize', 'open_panel'} <= present else '缺步骤'}
