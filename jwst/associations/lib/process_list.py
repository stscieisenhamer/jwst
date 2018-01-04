"""Reprocessing List"""


class ProcessList:
    """A Process list

    Parameters
    ----------
    items: [item[, ...]]
        The list of items to process

    rules: [Association[, ...]]
        List of rules to process the items against.

    work_over: int
        What the reprocessing should work on:
        - `ProcessList.EXISTING`: Only existing associations
        - `ProcessList.RULES`: Only on the rules to create new associations
        - `ProcessList.BOTH`: Compare to both existing and rules

    only_on_match: bool
        Only use this object if the overall condition
        is True.
    """

    (
        BOTH,
        EXISTING,
        RULES
    ) = range(1, 4)

    def __init__(self,
                 items=None,
                 rules=None,
                 work_over=BOTH,
                 only_on_match=False):
        self.items = items
        self.rules = rules
        self.work_over = work_over
        self.only_on_match = only_on_match
