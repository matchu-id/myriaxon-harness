"""`pygments.token` as a test double. See `pygments/__init__.py`."""

from . import _TokenNamespace

_ns = _TokenNamespace()
Token = _ns.Token
Text = _ns.Text
Whitespace = _ns.Whitespace
Error = _ns.Error
Other = _ns.Other
Keyword = _ns.Keyword
Name = _ns.Name
Literal = _ns.Literal
String = _ns.String
Number = _ns.Number
Operator = _ns.Operator
Punctuation = _ns.Punctuation
Comment = _ns.Comment
Generic = _ns.Generic

__all__ = ["Token", "Text", "Whitespace", "Error", "Other", "Keyword", "Name",
           "Literal", "String", "Number", "Operator", "Punctuation", "Comment", "Generic"]

