class SupplementError(Exception):
    pass


class CaseNotFoundError(SupplementError):
    pass


class CategoryNotSupportedError(SupplementError):
    pass


class CourtNoticeFolderMissingError(SupplementError):
    pass
