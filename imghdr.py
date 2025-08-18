# tiny shim for environments that do not have stdlib imghdr (e.g. Python 3.13+)
# The telegram library only calls imghdr.what(...) to detect image type.
# Returning None is fine — the library will still upload files.
def what(file, h=None):
    return None
  
