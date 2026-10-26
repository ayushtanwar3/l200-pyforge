"""AST-based static security analysis for generated Python scripts."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field


@dataclass(frozen=True)
class SecurityValidationResult:
    """Outcome of static AST security inspection."""

    is_safe: bool
    violations: list[str] = field(default_factory=list)


class ASTSecurityVisitor(ast.NodeVisitor):
    """AST node visitor enforcing PyForge's static code security boundaries."""

    PROHIBITED_BUILTINS: frozenset[str] = frozenset(
        {
            "eval",
            "exec",
            "compile",
            "__import__",
            "globals",
            "locals",
        }
    )

    PROHIBITED_NETWORK_MODULES: frozenset[str] = frozenset(
        {
            "socket",
            "urllib",
            "requests",
            "httpx",
            "aiohttp",
            "ftplib",
        }
    )

    PROHIBITED_SHELL_MODULES: frozenset[str] = frozenset(
        {
            "pty",
        }
    )

    SUBPROCESS_CALLS: frozenset[str] = frozenset(
        {
            "Popen",
            "run",
            "call",
            "check_call",
            "check_output",
        }
    )

    def __init__(self) -> None:
        self.violations: list[str] = []
        self._imported_aliases: dict[str, str] = {}

    def visit_Import(self, node: ast.Import) -> None:
        """Inspect `import ...` statements for prohibited modules."""
        for alias in node.names:
            root_module = alias.name.split(".")[0]
            bound_name = alias.asname or root_module
            self._imported_aliases[bound_name] = alias.name

            if root_module in self.PROHIBITED_NETWORK_MODULES:
                self.violations.append(
                    f"Line {node.lineno}: Prohibited network module import '{alias.name}'."
                )
            elif root_module in self.PROHIBITED_SHELL_MODULES:
                self.violations.append(
                    f"Line {node.lineno}: Prohibited shell module import '{alias.name}'."
                )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        """Inspect `from ... import ...` statements for prohibited modules or calls."""
        module_name = node.module or ""
        root_module = module_name.split(".")[0]

        if root_module in self.PROHIBITED_NETWORK_MODULES:
            self.violations.append(
                f"Line {node.lineno}: Prohibited network module import from '{module_name}'."
            )
        elif root_module in self.PROHIBITED_SHELL_MODULES:
            self.violations.append(
                f"Line {node.lineno}: Prohibited shell module import from '{module_name}'."
            )

        for alias in node.names:
            bound_name = alias.asname or alias.name
            full_target = f"{module_name}.{alias.name}" if module_name else alias.name
            self._imported_aliases[bound_name] = full_target

            if root_module == "os" and alias.name in {"system", "popen"}:
                self.violations.append(
                    f"Line {node.lineno}: Prohibited import of 'os.{alias.name}'."
                )
            if root_module in self.PROHIBITED_NETWORK_MODULES or (
                alias.name.split(".")[0] in self.PROHIBITED_NETWORK_MODULES
            ):
                self.violations.append(
                    f"Line {node.lineno}: Prohibited network module import '{full_target}'."
                )
            if root_module in self.PROHIBITED_SHELL_MODULES or (
                alias.name.split(".")[0] in self.PROHIBITED_SHELL_MODULES
            ):
                self.violations.append(
                    f"Line {node.lineno}: Prohibited shell module import '{full_target}'."
                )

        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        """Inspect function and method calls for security violations."""
        call_name = self._resolve_call_name(node.func)

        # 1. Check prohibited builtins (direct or via builtins.<func>)
        bare_name = call_name.split(".")[-1] if call_name else ""
        if call_name in self.PROHIBITED_BUILTINS or (
            call_name.startswith(("builtins.", "__builtins__."))
            and bare_name in self.PROHIBITED_BUILTINS
        ):
            self.violations.append(
                f"Line {node.lineno}: Prohibited builtin call '{call_name}()'."
            )

        # 2. Check arbitrary shell execution: os.system / os.popen / pty.spawn
        if call_name in {"os.system", "os.popen", "posix.system"}:
            self.violations.append(
                f"Line {node.lineno}: Prohibited shell execution call '{call_name}()'."
            )
        if call_name.startswith("pty."):
            self.violations.append(
                f"Line {node.lineno}: Prohibited pty call '{call_name}()'."
            )

        # 3. Check subprocess.*(..., shell=True)
        if self._is_subprocess_call(call_name):
            for kw in node.keywords:
                if kw.arg == "shell" and self._is_truthy_node(kw.value):
                    self.violations.append(
                        f"Line {node.lineno}: Prohibited '{call_name}(shell=True)' execution."
                    )

        # 4. Check destructive root filesystem wipe: shutil.rmtree('/')
        if call_name in {"shutil.rmtree", "rmtree"} and node.args:
            first_arg = node.args[0]
            if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
                cleaned_target = first_arg.value.strip()
                if cleaned_target in {"/", "/*"}:
                    self.violations.append(
                        f"Line {node.lineno}: Prohibited destructive filesystem call "
                        f"'{call_name}({first_arg.value!r})'."
                    )

        self.generic_visit(node)

    def _resolve_call_name(self, func: ast.AST) -> str:
        """Resolve an AST call target into a dotted string representation."""
        if isinstance(func, ast.Name):
            return self._imported_aliases.get(func.id, func.id)
        if isinstance(func, ast.Attribute):
            parent = self._resolve_call_name(func.value)
            if parent:
                return f"{parent}.{func.attr}"
            return func.attr
        return ""

    def _is_subprocess_call(self, call_name: str) -> bool:
        """Determine whether a resolved call name invokes a subprocess launcher."""
        if not call_name:
            return False
        parts = call_name.split(".")
        if parts[0] == "subprocess" and (
            len(parts) == 1 or parts[-1] in self.SUBPROCESS_CALLS
        ):
            return True
        if call_name in self.SUBPROCESS_CALLS:
            return True
        return False

    @staticmethod
    def _is_truthy_node(node: ast.AST) -> bool:
        """Return True if an AST expression is a constant truthy value or non-False expression."""
        if isinstance(node, ast.Constant):
            return bool(node.value)
        return True

    @classmethod
    def validate(cls, code: str) -> SecurityValidationResult:
        """Parse and validate Python source code against PyForge security rules.

        Args:
            code: Python source code string to inspect.

        Returns:
            SecurityValidationResult with `is_safe` and any detected `violations`.
        """
        if not code or not code.strip():
            return SecurityValidationResult(
                is_safe=False,
                violations=["Empty script code provided."],
            )

        try:
            tree = ast.parse(code)
        except SyntaxError as exc:
            return SecurityValidationResult(
                is_safe=False,
                violations=[
                    f"SyntaxError at line {exc.lineno}: {exc.msg}"
                ],
            )

        visitor = cls()
        visitor.visit(tree)
        return SecurityValidationResult(
            is_safe=len(visitor.violations) == 0,
            violations=visitor.violations,
        )


def validate_code_security(code: str) -> SecurityValidationResult:
    """Convenience function to validate Python source code using ASTSecurityVisitor."""
    return ASTSecurityVisitor.validate(code)

