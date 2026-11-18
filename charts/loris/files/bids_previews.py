"""Validate generated MRI preview files before reusing registered acquisitions."""

from PIL import Image
from sqlalchemy import select

from lib.db.models.file_parameter import DbFileParameter
from lib.db.models.parameter_type import DbParameterType


def preview_valid(db, file, root):
    value = db.execute(select(DbFileParameter.value).join(
        DbParameterType, DbParameterType.id == DbFileParameter.type_id).where(
            DbFileParameter.file_id == file.id,
            DbParameterType.name == 'check_pic_filename')).scalar_one_or_none()
    if not value:
        return False
    directory = (root / 'pic').resolve()
    path = (directory / value).resolve()
    if not path.is_relative_to(directory):
        raise ValueError('Registered preview escapes the preview directory')
    if not path.is_file():
        return False
    try:
        with Image.open(path) as image:
            if image.format != 'PNG':
                return False
            image.verify()
    except (OSError, SyntaxError):
        return False
    return True
