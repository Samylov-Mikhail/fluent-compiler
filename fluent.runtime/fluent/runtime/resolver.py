from __future__ import absolute_import, unicode_literals

import contextlib
from datetime import date, datetime
from decimal import Decimal

import attr
import six

from fluent.syntax.ast import (Attribute, AttributeExpression, CallExpression, Identifier, Message, MessageReference,
                               NumberLiteral, Pattern, Placeable, SelectExpression, StringLiteral, Term, TermReference,
                               TextElement, VariableReference, VariantExpression, VariantList)

from .errors import FluentCyclicReferenceError, FluentFormatError, FluentReferenceError
from .types import FluentDateType, FluentNone, FluentNumber, fluent_date, fluent_number
from .utils import (args_match, inspect_function_args, numeric_to_native,
                    reference_to_id, unknown_reference_error_obj)

try:
    from functools import singledispatch
except ImportError:
    # Python < 3.4
    from singledispatch import singledispatch


text_type = six.text_type

# Prevent expansion of too long placeables, for memory DOS protection
MAX_PART_LENGTH = 2500

# Prevent messages with too many sub parts, for CPI DOS protection
MAX_PARTS = 1000


# Unicode bidi isolation characters.
FSI = "\u2068"
PDI = "\u2069"


@attr.s
class CurrentEnvironment(object):
    # The parts of ResolverEnvironment that we want to mutate (and restore)
    # temporarily for some parts of a call chain.

    # The values of attributes here must not be mutated, they must only be
    # swapped out for different objects using `modified` (see below).

    # For Messages, VariableReference nodes are interpreted as external args,
    # but for Terms they are the values explicitly passed using CallExpression
    # syntax. So we have to be able to change 'args' for this purpose.
    args = attr.ib()
    # This controls whether we need to report an error if a VariableReference
    # refers to an arg that is not present in the args dict.
    error_for_missing_arg = attr.ib(default=True)


@attr.s
class ResolverEnvironment(object):
    context = attr.ib()
    errors = attr.ib()
    dirty = attr.ib(factory=set)
    part_count = attr.ib(default=0)
    current = attr.ib(factory=CurrentEnvironment)

    @contextlib.contextmanager
    def modified(self, **replacements):
        """
        Context manager that modifies the 'current' attribute of the
        environment, restoring the old data at the end.
        """
        # CurrentEnvironment only has args that we never mutate, so the shallow
        # copy returned by attr.evolve is fine (at least for now).
        old_current = self.current
        self.current = attr.evolve(old_current, **replacements)
        yield self
        self.current = old_current

    def modified_for_term_reference(self, args=None):
        return self.modified(args=args if args is not None else {},
                             error_for_missing_arg=False)


def resolve(context, message, args):
    """
    Given a FluentBundle, a Message instance and some arguments,
    resolve the message to a string.

    This is the normal entry point for this module.
    """
    errors = []
    env = ResolverEnvironment(context=context,
                              current=CurrentEnvironment(args=args),
                              errors=errors)
    return fully_resolve(message, env), errors


def fully_resolve(expr, env):
    """
    Fully resolve an expression to a string
    """
    # This differs from 'handle' in that 'handle' will often return non-string
    # objects, even if a string could have been returned, to allow for further
    # handling of that object e.g. attributes of messages. fully_resolve is
    # only used when we must have a string.
    retval = handle(expr, env)
    if isinstance(retval, text_type):
        return retval

    return fully_resolve(retval, env)


@singledispatch
def handle(expr, env):
    raise TypeError("Cannot handle object {0} of type {1}"
                    .format(expr, type(expr).__name__))


@handle.register(Message)
def handle_message(message, env):
    return handle(message.value, env)


@handle.register(Term)
def handle_term(term, env):
    return handle(term.value, env)


@handle.register(Pattern)
def handle_pattern(pattern, env):
    if pattern in env.dirty:
        env.errors.append(FluentCyclicReferenceError("Cyclic reference"))
        return FluentNone()

    env.dirty.add(pattern)

    parts = []
    use_isolating = env.context._use_isolating and len(pattern.elements) > 1

    for element in pattern.elements:
        env.part_count += 1
        if env.part_count > MAX_PARTS:
            if env.part_count == MAX_PARTS + 1:
                # Only append an error once.
                env.errors.append(ValueError("Too many parts in message (> {0}), "
                                             "aborting.".format(MAX_PARTS)))
                parts.append(fully_resolve(FluentNone(), env))
            break

        if isinstance(element, TextElement):
            # shortcut deliberately omits the FSI/PDI chars here.
            parts.append(element.value)
            continue

        part = fully_resolve(element, env)
        if use_isolating:
            parts.append(FSI)
        if len(part) > MAX_PART_LENGTH:
            env.errors.append(ValueError(
                "Too many characters in part, "
                "({0}, max allowed is {1})".format(len(part),
                                                   MAX_PART_LENGTH)))
            part = part[:MAX_PART_LENGTH]
        parts.append(part)
        if use_isolating:
            parts.append(PDI)
    retval = "".join(parts)
    env.dirty.remove(pattern)
    return retval


@handle.register(TextElement)
def handle_text_element(text_element, env):
    return text_element.value


@handle.register(Placeable)
def handle_placeable(placeable, env):
    return handle(placeable.expression, env)


@handle.register(StringLiteral)
def handle_string_expression(string_expression, env):
    return string_expression.value


@handle.register(NumberLiteral)
def handle_number_expression(number_expression, env):
    return numeric_to_native(number_expression.value)


@handle.register(MessageReference)
def handle_message_reference(message_reference, env):
    return handle(lookup_reference(message_reference, env), env)


@handle.register(TermReference)
def handle_term_reference(term_reference, env):
    with env.modified_for_term_reference():
        return handle(lookup_reference(term_reference, env), env)


def lookup_reference(ref, env):
    """
    Given a MessageReference, TermReference or AttributeExpression, returns the
    AST node, or FluentNone if not found, including fallback logic
    """
    ref_id = reference_to_id(ref)

    try:
        return env.context._messages_and_terms[ref_id]
    except LookupError:
        env.errors.append(unknown_reference_error_obj(ref_id))

        if isinstance(ref, AttributeExpression):
            # Fallback
            parent_id = reference_to_id(ref.ref)
            try:
                return env.context._messages_and_terms[parent_id]
            except LookupError:
                # Don't add error here, because we already added error for the
                # actual thing we were looking for.
                pass

    return FluentNone(ref_id)


@handle.register(FluentNone)
def handle_fluent_none(none, env):
    return none.format(env.context._babel_locale)


@handle.register(type(None))
def handle_none(none, env):
    # We raise the same error type here as when a message is completely missing.
    raise LookupError("Message body not defined")


@handle.register(VariableReference)
def handle_variable_reference(argument, env):
    name = argument.id.name
    try:
        arg_val = env.current.args[name]
    except LookupError:
        if env.current.error_for_missing_arg:
            env.errors.append(
                FluentReferenceError("Unknown external: {0}".format(name)))
        return FluentNone(name)

    # The code below should be synced with fluent.runtime.runtime.handle_argument
    if isinstance(arg_val,
                  (int, float, Decimal,
                   date, datetime,
                   text_type)):
        return arg_val
    env.errors.append(TypeError("Unsupported external type: {0}, {1}"
                                .format(name, type(arg_val))))
    return FluentNone(name)


@handle.register(AttributeExpression)
def handle_attribute_expression(attribute_ref, env):
    return handle(lookup_reference(attribute_ref, env), env)


@handle.register(Attribute)
def handle_attribute(attribute, env):
    return handle(attribute.value, env)


@handle.register(VariantList)
def handle_variant_list(variant_list, env):
    return select_from_variant_list(variant_list, env, None)


def select_from_variant_list(variant_list, env, key):
    found = None
    for variant in variant_list.variants:
        if variant.default:
            default = variant
            if key is None:
                # We only want the default
                break

        compare_value = handle(variant.key, env)
        if match(key, compare_value, env):
            found = variant
            break

    if found is None:
        if (key is not None and not isinstance(key, FluentNone)):
            env.errors.append(FluentReferenceError("Unknown variant: {0}"
                                                   .format(key)))
        found = default
    if found is None:
        return FluentNone()

    return handle(found.value, env)


@handle.register(SelectExpression)
def handle_select_expression(expression, env):
    key = handle(expression.selector, env)
    return select_from_select_expression(expression, env,
                                         key=key)


def select_from_select_expression(expression, env, key):
    default = None
    found = None
    for variant in expression.variants:
        if variant.default:
            default = variant

        compare_value = handle(variant.key, env)
        if match(key, compare_value, env):
            found = variant
            break

    if found is None:
        found = default
    if found is None:
        return FluentNone()
    return handle(found.value, env)


def is_number(val):
    return isinstance(val, (int, float))


def match(val1, val2, env):
    if val1 is None or isinstance(val1, FluentNone):
        return False
    if val2 is None or isinstance(val2, FluentNone):
        return False
    if is_number(val1):
        if not is_number(val2):
            # Could be plural rule match
            return env.context._plural_form(val1) == val2
    elif is_number(val2):
        return match(val2, val1, env)

    return val1 == val2


@handle.register(Identifier)
def handle_indentifier(identifier, env):
    return identifier.name


@handle.register(VariantExpression)
def handle_variant_expression(expression, env):
    message = lookup_reference(expression.ref, env)
    if isinstance(message, FluentNone):
        return message

    # TODO What to do if message is not a VariantList?
    # Need test at least.
    assert isinstance(message.value, VariantList)

    variant_name = expression.key.name
    return select_from_variant_list(message.value,
                                    env,
                                    variant_name)


@handle.register(CallExpression)
def handle_call_expression(expression, env):
    args = [handle(arg, env) for arg in expression.positional]
    kwargs = {kwarg.name.name: handle(kwarg.value, env) for kwarg in expression.named}

    if isinstance(expression.callee, (TermReference, AttributeExpression)):
        term = lookup_reference(expression.callee, env)
        if args:
            env.errors.append(FluentFormatError("Ignored positional arguments passed to term '{0}'"
                                                .format(reference_to_id(expression.callee))))
        with env.modified_for_term_reference(args=kwargs):
            return handle(term, env)

    # builtin or custom function call
    function_name = expression.callee.id.name
    try:
        function = env.context._functions[function_name]
    except LookupError:
        env.errors.append(FluentReferenceError("Unknown function: {0}"
                                               .format(function_name)))
        return FluentNone(function_name + "()")

    arg_spec = inspect_function_args(function, function_name, env.errors)
    match, sanitized_args, sanitized_kwargs, errors = args_match(function_name, args, kwargs, arg_spec)
    env.errors.extend(errors)
    if match:
        return function(*sanitized_args, **sanitized_kwargs)
    return FluentNone(function_name + "()")


@handle.register(FluentNumber)
def handle_fluent_number(number, env):
    return number.format(env.context._babel_locale)


@handle.register(int)
def handle_int(integer, env):
    return fluent_number(integer).format(env.context._babel_locale)


@handle.register(float)
def handle_float(f, env):
    return fluent_number(f).format(env.context._babel_locale)


@handle.register(Decimal)
def handle_decimal(d, env):
    return fluent_number(d).format(env.context._babel_locale)


@handle.register(FluentDateType)
def handle_fluent_date_type(d, env):
    return d.format(env.context._babel_locale)


@handle.register(date)
def handle_date(d, env):
    return fluent_date(d).format(env.context._babel_locale)


@handle.register(datetime)
def handle_datetime(d, env):
    return fluent_date(d).format(env.context._babel_locale)
