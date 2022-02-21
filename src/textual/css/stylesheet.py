from __future__ import annotations

import os
from collections import defaultdict
from operator import itemgetter
import os
from typing import cast, Iterable


import rich.repr
from rich.console import Group, RenderableType
from rich.highlighter import ReprHighlighter
from rich.padding import Padding
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

from textual._loop import loop_last
from .._context import active_app
from .errors import StylesheetError
from .match import _check_selectors
from .model import RuleSet
from .parse import parse
from .styles import RULE_NAMES, Styles, RulesMap
from .types import Specificity3, Specificity4
from ..dom import DOMNode
from .. import log


class StylesheetParseError(Exception):
    def __init__(self, errors: StylesheetErrors) -> None:
        self.errors = errors

    def __rich__(self) -> RenderableType:
        return self.errors


class StylesheetErrors:
    def __init__(self, stylesheet: "Stylesheet") -> None:
        self.stylesheet = stylesheet

    @classmethod
    def _get_snippet(cls, code: str, line_no: int) -> Panel:
        syntax = Syntax(
            code,
            lexer="scss",
            theme="ansi_light",
            line_numbers=True,
            indent_guides=True,
            line_range=(max(0, line_no - 2), line_no + 1),
            highlight_lines={line_no},
        )
        return Panel(syntax, border_style="red")

    def __rich__(self) -> RenderableType:
        highlighter = ReprHighlighter()
        errors: list[RenderableType] = []
        append = errors.append
        for rule in self.stylesheet.rules:
            for token, message in rule.errors:
                append("")
                append(Text(" Error in stylesheet:", style="bold red"))

                if token.referenced_by:
                    line_idx, col_idx = token.referenced_by.location
                    line_no, col_no = line_idx + 1, col_idx + 1
                    append(
                        highlighter(f" {token.path or '<unknown>'}:{line_no}:{col_no}")
                    )
                    append(self._get_snippet(token.code, line_no))
                else:
                    line_idx, col_idx = token.location
                    line_no, col_no = line_idx + 1, col_idx + 1
                    append(
                        highlighter(f" {token.path or '<unknown>'}:{line_no}:{col_no}")
                    )
                    append(self._get_snippet(token.code, line_no))

                final_message = ""
                for is_last, message_part in loop_last(message.split(";")):
                    end = "" if is_last else "\n"
                    final_message += f"• {message_part.strip()};{end}"

                append(Padding(highlighter(Text(final_message, "red")), pad=(0, 1)))
                append("")
        return Group(*errors)


@rich.repr.auto
class Stylesheet:
    def __init__(self) -> None:
        self.rules: list[RuleSet] = []

    def __rich_repr__(self) -> rich.repr.Result:
        yield self.rules

    @property
    def css(self) -> str:
        return "\n\n".join(rule_set.css for rule_set in self.rules)

    @property
    def any_errors(self) -> bool:
        """Check if there are any errors."""
        return any(rule.errors for rule in self.rules)

    @property
    def error_renderable(self) -> StylesheetErrors:
        return StylesheetErrors(self)

    def read(self, filename: str) -> None:
        filename = os.path.expanduser(filename)
        try:
            with open(filename, "rt") as css_file:
                css = css_file.read()
            path = os.path.abspath(filename)
        except Exception as error:
            raise StylesheetError(f"unable to read {filename!r}; {error}")
        try:
            rules = list(parse(css, path))
        except Exception as error:
            raise StylesheetError(f"failed to parse {filename!r}; {error}")
        self.rules.extend(rules)

    def parse(self, css: str, *, path: str = "") -> None:
        try:
            rules = list(parse(css, path))
        except Exception as error:
            raise StylesheetError(f"failed to parse css; {error}")
        self.rules.extend(rules)
        if self.any_errors:
            raise StylesheetParseError(self.error_renderable)

    @classmethod
    def _check_rule(cls, rule: RuleSet, node: DOMNode) -> Iterable[Specificity3]:
        for selector_set in rule.selector_set:
            if _check_selectors(selector_set.selectors, node):
                yield selector_set.specificity

    def apply(self, node: DOMNode, animate: bool = False) -> None:
        """Apply the stylesheet to a DOM node.

        Args:
            node (DOMNode): The ``DOMNode`` to apply the stylesheet to.
                Applies the styles defined in this ``Stylesheet`` to the node.
                If the same rule is defined multiple times for the node (e.g. multiple
                classes modifying the same CSS property), then only the most specific
                rule will be applied.
            animate (bool, optional): Animate changed rules. Defaults to ``False``.
        """

        # Dictionary of rule attribute names e.g. "text_background" to list of tuples.
        # The tuples contain the rule specificity, and the value for that rule.
        # We can use this to determine, for a given rule, whether we should apply it
        # or not by examining the specificity. If we have two rules for the
        # same attribute, then we can choose the most specific rule and use that.
        rule_attributes: dict[str, list[tuple[Specificity4, object]]]
        rule_attributes = defaultdict(list)

        _check_rule = self._check_rule

        # Collect default node CSS rules
        for key, default_specificity, value in node._default_rules:
            rule_attributes[key].append((default_specificity, value))

        # Collect the rules defined in the stylesheet
        for rule in self.rules:
            for specificity in _check_rule(rule, node):
                for key, rule_specificity, value in rule.styles.extract_rules(
                    specificity
                ):
                    rule_attributes[key].append((rule_specificity, value))

        # For each rule declared for this node, keep only the most specific one
        get_first_item = itemgetter(0)
        node_rules: RulesMap = cast(
            RulesMap,
            {
                name: max(specificity_rules, key=get_first_item)[1]
                for name, specificity_rules in rule_attributes.items()
            },
        )

        self.replace_rules(node, node_rules, animate=animate)

    @classmethod
    def replace_rules(
        cls, node: DOMNode, rules: RulesMap, animate: bool = False
    ) -> None:
        """Replace style rules on a node, animating as required.

        Args:
            node (DOMNode): A DOM node.
            rules (RulesMap): Mapping of rules.
            animate (bool, optional): Enable animation. Defaults to False.
        """

        # Alias styles and base styles
        styles = node.styles
        base_styles = styles.base

        # Styles currently used an new rules
        modified_rule_keys = {*base_styles.get_rules().keys(), *rules.keys()}
        # Current render rules (missing rules are filled with default)
        current_render_rules = styles.get_render_rules()

        # Calculate replacement rules (defaults + new rules)
        new_styles = node._default_styles.copy()
        new_styles.merge_rules(rules)

        if new_styles == base_styles:
            # Nothing to change, return early
            return

        # New render rules
        new_render_rules = new_styles.get_render_rules()

        # Some aliases
        is_animatable = styles.is_animatable
        get_current_render_rule = current_render_rules.get
        get_new_render_rule = new_render_rules.get

        if animate:
            for key in modified_rule_keys:
                # Get old and new render rules
                old_render_value = get_current_render_rule(key)
                new_render_value = get_new_render_rule(key)
                # Get new rule value (may be None)
                new_value = rules.get(key)

                # Check if this can / should be animated
                if is_animatable(key) and new_render_value != old_render_value:
                    transition = new_styles.get_transition(key)
                    if transition is not None:
                        duration, easing, delay = transition
                        node.app.animator.animate(
                            node.styles.base,
                            key,
                            new_render_value,
                            final_value=new_value,
                            duration=duration,
                            easing=easing,
                        )
                        continue
                # Default is to set value (if new_value is None, rule will be removed)
                setattr(base_styles, key, new_value)
        else:
            # Not animated, so we apply the rules directly
            for key in modified_rule_keys:
                setattr(base_styles, key, rules.get(key))

        # The styles object may have requested a refresh / layout
        # It's the style properties that set these flags
        repaint, layout = styles.check_refresh()
        if repaint:
            node.refresh(layout=layout)

    def update(self, root: DOMNode, animate: bool = False) -> None:
        """Update a node and its children."""
        apply = self.apply
        for node in root.walk_children():
            apply(node, animate=animate)


if __name__ == "__main__":
    from rich.traceback import install

    install(show_locals=True)

    class Widget(DOMNode):
        pass

    class View(DOMNode):
        pass

    class App(DOMNode):
        pass

    app = App()
    main_view = View(id="main")
    help_view = View(id="help")
    app.add_child(main_view)
    app.add_child(help_view)

    widget1 = Widget(id="widget1")
    widget2 = Widget(id="widget2")
    sidebar = Widget(id="sidebar")
    sidebar.add_class("float")

    helpbar = Widget(id="helpbar")
    helpbar.add_class("float")

    main_view.add_child(widget1)
    main_view.add_child(widget2)
    main_view.add_child(sidebar)

    sub_view = View(id="sub")
    sub_view.add_class("-subview")
    main_view.add_child(sub_view)

    tooltip = Widget(id="tooltip")
    tooltip.add_class("float", "transient")
    sub_view.add_child(tooltip)

    help = Widget(id="markdown")
    help_view.add_child(help)
    help_view.add_child(helpbar)

    from rich import print

    print(app.tree)
    print()

    CSS = """
    App > View {
        layout: dock;
        docks: sidebar=left | widgets=top;
    }

    #sidebar {
        dock-group: sidebar;
    }

    #widget1 {
        text: on blue;
        dock-group: widgets;
    }

    #widget2 {
        text: on red;
        dock-group: widgets;
    }

    """

    stylesheet = Stylesheet()
    stylesheet.parse(CSS)

    print(stylesheet.css)

    # print(stylesheet.error_renderable)

    # print(widget1.styles)

    # stylesheet.apply(widget1)

    # print(widget1.styles)

    # print(stylesheet.css)

    # from .query import DOMQuery

    # tests = ["View", "App > View", "Widget.float", ".float.transient", "*"]

    # for test in tests:
    #     print("")
    #     print(f"[b]{test}")
    #     print(app.query(test))
