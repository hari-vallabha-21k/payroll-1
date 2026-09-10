"""Safe evaluation of payroll formulas.

Formulas are configuration written by administrators, so they are parsed into
an AST and walked with an explicit whitelist of node types. There is no eval,
no exec, no attribute access, no imports, no indexing and no calls other than
the named helpers below - a formula cannot reach anything outside the values
handed to it.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from decimal import Decimal, DivisionByZero, InvalidOperation

MAX_FORMULA_LENGTH = 500


class FormulaError(Exception):
    """Raised for a formula that is malformed, unsafe, or unresolvable."""


@dataclass(frozen=True)
class Variable:
    code: str
    label: str
    description: str


# Values the engine puts in scope for every formula. Rule codes are added on
# top of these as each rule is evaluated.
BUILTIN_VARIABLES: tuple[Variable, ...] = (
    Variable("MONTHLY_SALARY", "Monthly Salary", "Contracted monthly salary for the employee"),
    Variable("DAILY_SALARY", "Daily Salary", "Monthly salary divided by the payroll day basis"),
    Variable("HOURLY_RATE", "Hourly Rate", "Daily salary divided by standard hours per day"),
    Variable("TOTAL_DAYS", "Total Days", "Calendar days in the pay period"),
    Variable("WORKING_DAYS", "Working Days", "Days the employee was liable to work"),
    Variable("PAYABLE_DAYS", "Payable Days", "Days that attract pay, including paid leave"),
    Variable("PRESENT_DAYS", "Present Days", "Days actually worked (half days count as 0.5)"),
    Variable("ABSENT_DAYS", "Absent Days", "Days absent without approved leave"),
    Variable("PAID_LEAVE", "Paid Leave", "Approved paid leave days"),
    Variable("UNPAID_LEAVE", "Unpaid Leave", "Approved unpaid leave days"),
    Variable("LOP_DAYS", "LOP Days", "Loss-of-pay days"),
    Variable("OT_HOURS", "Overtime Hours", "Overtime hours from processed attendance"),
    Variable("OT_RATE", "Overtime Rate", "Configured overtime rate per hour"),
    Variable("DAY_BASIS", "Day Basis", "Divisor used for daily salary, e.g. 26 or 30"),
    Variable("STANDARD_HOURS", "Standard Hours", "Standard working hours in a day"),
)

BUILTIN_CODES = frozenset(v.code for v in BUILTIN_VARIABLES)

# Names produced by the engine as it goes, not supplied up front.
DERIVED_CODES = frozenset({"GROSS", "TOTAL_EARNINGS", "TOTAL_DEDUCTIONS", "NET"})


def _round(value: Decimal, places: Decimal | int = 2) -> Decimal:
    quantum = Decimal(1).scaleb(-int(places))
    return value.quantize(quantum)


def _if(condition: Decimal, when_true: Decimal, when_false: Decimal) -> Decimal:
    return when_true if condition != 0 else when_false


FUNCTIONS = {
    "MIN": lambda *args: min(args),
    "MAX": lambda *args: max(args),
    "ROUND": _round,
    "ABS": abs,
    "IF": _if,
    "FLOOR": lambda value: Decimal(int(value // 1)),
    "CEIL": lambda value: Decimal(-int(-value // 1)),
}

_ALLOWED_BINOPS = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.Mod: lambda a, b: a % b,
    ast.Pow: lambda a, b: a**b,
}

_ALLOWED_COMPARE = {
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
    ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b,
    ast.Gt: lambda a, b: a > b,
    ast.GtE: lambda a, b: a >= b,
}


@dataclass
class ValidationResult:
    ok: bool
    error: str | None = None
    variables_used: list[str] = field(default_factory=list)
    functions_used: list[str] = field(default_factory=list)


def _to_decimal(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return Decimal(1) if value else Decimal(0)
    if isinstance(value, (int, float, str)):
        return Decimal(str(value))
    raise FormulaError(f"Unsupported value in formula: {value!r}")


def _evaluate_node(node: ast.AST, scope: dict[str, Decimal]) -> Decimal:
    if isinstance(node, ast.Expression):
        return _evaluate_node(node.body, scope)

    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise FormulaError("Only numeric constants are allowed")
        return Decimal(str(node.value))

    if isinstance(node, ast.Name):
        if node.id not in scope:
            raise FormulaError(f"Unknown variable '{node.id}'")
        return _to_decimal(scope[node.id])

    if isinstance(node, ast.BinOp):
        operator = _ALLOWED_BINOPS.get(type(node.op))
        if operator is None:
            raise FormulaError(f"Operator {type(node.op).__name__} is not allowed")
        left = _evaluate_node(node.left, scope)
        right = _evaluate_node(node.right, scope)
        try:
            return _to_decimal(operator(left, right))
        except (DivisionByZero, ZeroDivisionError):
            # A zero divisor is a data condition, not a broken formula.
            return Decimal(0)
        except (InvalidOperation, OverflowError) as exc:
            raise FormulaError(f"Arithmetic error: {exc}") from exc

    if isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.USub):
            return -_evaluate_node(node.operand, scope)
        if isinstance(node.op, ast.UAdd):
            return _evaluate_node(node.operand, scope)
        raise FormulaError("Only + and - may prefix a value")

    if isinstance(node, ast.Compare):
        if len(node.ops) != 1:
            raise FormulaError("Only a single comparison is allowed")
        comparison = _ALLOWED_COMPARE.get(type(node.ops[0]))
        if comparison is None:
            raise FormulaError("That comparison is not allowed")
        left = _evaluate_node(node.left, scope)
        right = _evaluate_node(node.comparators[0], scope)
        return Decimal(1) if comparison(left, right) else Decimal(0)

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise FormulaError("Only the built-in payroll functions may be called")
        name = node.func.id
        if name not in FUNCTIONS:
            raise FormulaError(f"Unknown function '{name}'")
        if node.keywords:
            raise FormulaError("Functions take positional arguments only")
        arguments = [_evaluate_node(argument, scope) for argument in node.args]
        try:
            return _to_decimal(FUNCTIONS[name](*arguments))
        except FormulaError:
            raise
        except Exception as exc:
            raise FormulaError(f"{name}() failed: {exc}") from exc

    raise FormulaError(f"{type(node).__name__} is not allowed in a formula")


def parse(formula: str) -> ast.Expression:
    if not formula or not formula.strip():
        raise FormulaError("Formula is empty")
    if len(formula) > MAX_FORMULA_LENGTH:
        raise FormulaError(f"Formula is longer than {MAX_FORMULA_LENGTH} characters")
    try:
        return ast.parse(formula, mode="eval")
    except SyntaxError as exc:
        raise FormulaError(f"Syntax error: {exc.msg}") from exc


def names_used(formula: str) -> tuple[set[str], set[str]]:
    """Variable names and function names a formula refers to."""
    tree = parse(formula)
    variables: set[str] = set()
    functions: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            functions.add(node.func.id)
        elif isinstance(node, ast.Name):
            variables.add(node.id)
    return variables - functions, functions


def validate(formula: str, known_variables: set[str] | None = None) -> ValidationResult:
    """Check a formula before it is saved.

    Evaluates it once against zeroed variables, so an unsafe construct or an
    unknown name is rejected at configuration time rather than at payroll time.
    """
    try:
        variables, functions = names_used(formula)
    except FormulaError as exc:
        return ValidationResult(False, str(exc))

    unknown_functions = functions - set(FUNCTIONS)
    if unknown_functions:
        return ValidationResult(
            False, f"Unknown function(s): {', '.join(sorted(unknown_functions))}"
        )

    allowed = set(known_variables or set()) | BUILTIN_CODES | DERIVED_CODES
    unknown = variables - allowed
    if unknown:
        return ValidationResult(False, f"Unknown variable(s): {', '.join(sorted(unknown))}")

    probe = {name: Decimal(0) for name in allowed}
    try:
        _evaluate_node(parse(formula), probe)
    except FormulaError as exc:
        return ValidationResult(False, str(exc))

    return ValidationResult(
        True, variables_used=sorted(variables), functions_used=sorted(functions)
    )


def evaluate(formula: str, scope: dict[str, Decimal]) -> Decimal:
    """Evaluate a formula against a scope of Decimal values."""
    return _evaluate_node(parse(formula), scope)
