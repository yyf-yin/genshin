"""`when` 条件和整队限制的安全求值。

只支持比较、and/or/not、常量、列表、上下文变量和白名单函数，不执行任意代码。
例：
    cons >= 1
    count('anemo') == 2 and max_count(['pyro', 'cryo', 'electro', 'hydro']) == 2
"""

from __future__ import annotations

import ast
import operator
from typing import Any, Callable, Mapping

_CMP = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}


class ConditionError(ValueError):
    pass


def evaluate(expr: str, names: Mapping[str, Any], funcs: Mapping[str, Callable] | None = None) -> bool:
    funcs = funcs or {}
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise ConditionError(f"条件语法错误：{expr!r}") from e

    def ev(node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, (ast.List, ast.Tuple)):
            return [ev(e) for e in node.elts]
        if isinstance(node, ast.Name):
            if node.id not in names:
                raise ConditionError(f"未知变量 {node.id!r}：{expr!r}")
            return names[node.id]
        if isinstance(node, ast.BoolOp):
            vals = (ev(v) for v in node.values)
            return all(vals) if isinstance(node.op, ast.And) else any(vals)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return not ev(node.operand)
        if isinstance(node, ast.Compare):
            left = ev(node.left)
            for op, comp in zip(node.ops, node.comparators):
                right = ev(comp)
                if type(op) not in _CMP or not _CMP[type(op)](left, right):
                    return False
                left = right
            return True
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords:
            fn = funcs.get(node.func.id)
            if fn is None:
                raise ConditionError(f"不支持的函数 {node.func.id!r}：{expr!r}")
            return fn(*[ev(a) for a in node.args])
        raise ConditionError(f"不支持的写法 {type(node).__name__}：{expr!r}")

    return bool(ev(tree))


def check(expr: str | None, names: Mapping[str, Any]) -> bool:
    """`when` 为空表示总是生效。"""
    return True if not expr else evaluate(expr, names)
