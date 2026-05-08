from enum import StrEnum as IStrEnum


class StrEnum(IStrEnum, str):
    pass
