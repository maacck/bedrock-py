import types
from collections import namedtuple
from collections.abc import Iterable
from datetime import datetime
from inspect import signature
from itertools import chain
from typing import Union

from sqlalchemy import (
    DateTime,
    and_,
    func,
    inspect,
    or_,
)
from sqlalchemy.ext.associationproxy import AssociationProxyExtensionType
from sqlalchemy.ext.hybrid import HybridExtensionType
from sqlalchemy.orm import InstrumentedAttribute, RelationshipProperty
from sqlalchemy.sql.type_api import TypeEngine

from bedrock.exc import BadFilterFormatError

BooleanFunction = namedtuple("BooleanFunction", ("key", "sqlalchemy_fn", "only_one_arg"))
BOOLEAN_FUNCTIONS = [
    BooleanFunction("or", or_, False),
    BooleanFunction("and", and_, False),
]


def get_model_relationship(model, field) -> RelationshipProperty:
    model_state = inspect(model)
    try:
        return model_state.relationships[field]
    except KeyError as err:
        raise BadFilterFormatError(f"Model {model.__name__} has no relationship `{field}`.") from err


class Field:
    def __init__(self, model, field_name):
        self.model = model
        self.field_name = field_name
        self.model_state = inspect(model)
        self.join_args = []
        self.field: Field | None = None
        if "." in self.field_name:
            field_parts = self.field_name.split(".")
            relationship_name = field_parts[0]
            self.join_path = field_parts
            if relationship_name in self.model_state.relationships.keys():
                relationship_state = self.model_state.relationships[relationship_name]
                relationship_model = relationship_state.mapper.class_
                self.field = Field(relationship_model, ".".join(field_parts[1:]))
                self.join_args = [
                    (
                        relationship_model,
                        getattr(self.model, relationship_name),
                    )
                ]
            else:
                raise BadFilterFormatError(f"Invalid filter key: {self.field_name}")

    def get_sqlalchemy_field(self):
        if self.field:
            return self.field.get_sqlalchemy_field()
        if self.field_name not in self._get_valid_field_names():
            raise BadFilterFormatError(f"Model {self.model.__name__} has no column `{self.field_name}`.")
        sqlalchemy_field = getattr(self.model, self.field_name)

        # If it's a hybrid method, then we call it so that we can work with
        # the result of the execution and not with the method object itself
        if isinstance(sqlalchemy_field, types.MethodType):
            sqlalchemy_field = sqlalchemy_field()

        return sqlalchemy_field

    def _get_valid_field_names(self):
        columns = self.model_state.columns
        orm_descriptors = self.model_state.all_orm_descriptors
        relationship_names = self.model_state.relationships.keys()
        column_names = columns.keys()
        hybrid_names = [
            key
            for key, item in orm_descriptors.items()
            if _is_hybrid_property(item)
            or _is_hybrid_method(item)
            or _is_association_column(item)
            or key in relationship_names
        ]
        return set(column_names) | set(hybrid_names)

    def get_join_args(self) -> list:
        if self.join_args and isinstance(self.field, Field):
            return [*self.join_args, *self.field.get_join_args()]
        elif self.join_args:
            return self.join_args
        return []

    def get_sql_type(self, dialect=None):
        """
        Get the SQL type of the field at runtime.

        Args:
            dialect: Optional SQLAlchemy dialect. If provided, returns compiled SQL string.
                    If None, returns the TypeEngine object.

        Returns:
            TypeEngine object or SQL string representation if dialect is provided.
        """
        sqlalchemy_field = self.get_sqlalchemy_field()

        # For InstrumentedAttribute, get the column type
        if isinstance(sqlalchemy_field, InstrumentedAttribute):
            # Get the column from the property
            column = sqlalchemy_field.property.columns[0]
            sql_type = column.type

            # If dialect is provided, compile to SQL string
            if dialect is not None:
                return sql_type.compile(dialect=dialect)

            return sql_type

        # For hybrid properties/methods, try to get type from expression
        if hasattr(sqlalchemy_field, "property") and hasattr(sqlalchemy_field.property, "expression"):
            expr = sqlalchemy_field.property.expression
            if hasattr(expr, "type"):
                sql_type = expr.type
                if dialect is not None:
                    return sql_type.compile(dialect=dialect)
                return sql_type

        raise BadFilterFormatError(f"Cannot determine SQL type for field `{self.field_name}`.")


def _is_hybrid_property(orm_descriptor):
    return orm_descriptor.extension_type == HybridExtensionType.HYBRID_PROPERTY


def _is_association_column(orm_descriptor):
    return orm_descriptor.extension_type == AssociationProxyExtensionType.ASSOCIATION_PROXY


def _is_hybrid_method(orm_descriptor):
    return orm_descriptor.extension_type == HybridExtensionType.HYBRID_METHOD


class Operator:
    OPERATORS = {
        "is_null": lambda f: f.is_(None),
        "is_not_null": lambda f: f.is_not(None),
        "==": lambda f, a: f == a,
        "eq": lambda f, a: f == a,
        "!=": lambda f, a: f != a,
        "neq": lambda f, a: f != a,
        ">": lambda f, a: f > a,
        "gt": lambda f, a: f > a,
        "<": lambda f, a: f < a,
        "lt": lambda f, a: f < a,
        ">=": lambda f, a: f >= a,
        "ge": lambda f, a: f >= a,
        "<=": lambda f, a: f <= a,
        "le": lambda f, a: f <= a,
        "like": lambda f, a: f.like(a),
        "ilike": lambda f, a: f.ilike(a),
        "not_ilike": lambda f, a: ~f.ilike(a),
        "in": lambda f, a: f.in_(a),
        "not_in": lambda f, a: ~f.in_(a),
        "any": lambda f, a: f.any(a),
        "not_any": lambda f, a: func.not_(f.any(a)),
        "between": lambda f, a: f.between(a[0], a[1]),
        "has": lambda f, a: f.has(a),
        "text_search": lambda f, a: f.text_search(a),
        "fuzzy_search": lambda f, a: f.ilike("%" + a + "%"),
    }

    def __init__(self, operator=None, negate=False):
        if not operator:
            operator = "=="

        if operator not in self.OPERATORS:
            raise BadFilterFormatError(f"Operator `{operator}` not valid.")
        self.operator = operator
        self.function = self.OPERATORS[operator]
        self.arity = len(signature(self.function).parameters)
        self.negate = negate
        if negate:
            if self.arity == 1:
                self.function = lambda f: ~self.OPERATORS[operator](f)
            else:
                self.function = lambda f, a: ~self.OPERATORS[operator](f, a)

    def __str__(self):
        if self.negate:
            return f"~{self.operator}"
        return self.operator


class Filter:
    def __init__(self, filter_spec, strategy="auto"):
        self.filter_spec = filter_spec
        # when nested_strategy is auto, it will automatically decide to use any() or has()
        self.strategy = strategy
        try:
            filter_spec["field"]
        except KeyError as err:
            raise BadFilterFormatError("`field` is a mandatory filter attribute.") from err
        except TypeError as err:
            raise BadFilterFormatError(f"Filter spec `{filter_spec}` should be a dictionary.") from err
        self.negate = False
        field_name = filter_spec["field"]
        if field_name.startswith("!"):
            field_name = field_name[1:]
            self.negate = True

        self.field = field_name
        self.operator = Operator(filter_spec.get("op", "eq"), negate=self.negate)
        self.value = filter_spec.get("value")
        self.join_path = []
        value_present = "value" in filter_spec
        if not value_present and self.operator.arity == 2:
            raise BadFilterFormatError("`value` must be provided.")
        # Two types of nested field scenarios:
        # 1. rel_field:rel_field:text_field (colon) -> use any() or has()
        # 2. rel_field.rel_field.text_field (dot) -> join the relationship model
        if isinstance(self.value, dict) and "op" in self.value:
            self.value = Filter(self.value)
        if "." in field_name:
            self.strategy = "join"

        elif ":" in field_name:
            field_parts = field_name.split(":")
            self.join_path = field_parts
            self.field = field_parts[0]
            self.operator = None
            self.value = Filter(
                {
                    "op": filter_spec.get("op", "eq"),
                    "field": ":".join(field_parts[1:]),
                    "value": filter_spec.get("value"),
                }
            )

    def get_named_models(self):
        if "model" in self.filter_spec:
            return {self.filter_spec["model"]}
        return set()

    def get_join_args(self, model):
        if "." in self.field:
            return Field(model, self.filter_spec["field"]).get_join_args()
        return []

    def _format_value(self, sql_type: TypeEngine, value):
        # if sqlalchemy_field is a field
        # and if sqlalchemy_field type is Datetime
        # convert value to datetime
        if isinstance(sql_type, DateTime):
            if isinstance(value, datetime):
                return value
            if isinstance(value, str):
                value = datetime.fromisoformat(value)
            elif isinstance(value, int):
                value = datetime.fromtimestamp(value)
            else:
                raise BadFilterFormatError(f"Value `{value}` is not a valid datetime format.")
        return value

    def _format_text_search(self, default_model):
        """Handle text_search operator by wrapping in any()/has() based on relationship type."""
        field = Field(default_model, self.field)
        relationship = get_model_relationship(default_model, self.field)
        if relationship.uselist:
            wrapper = Operator("any", negate=self.negate)
        else:
            wrapper = Operator("has", negate=self.negate)
        return wrapper.function(
            field.get_sqlalchemy_field(),
            self.operator.function(relationship.mapper.class_, self.value),
        )

    def format_for_sqlalchemy(self, default_model):
        operator = self.operator
        value = self.value

        if str(self.operator) == "text_search":
            return self._format_text_search(default_model)

        # auto determine if it is any or has
        if isinstance(value, Filter) and self.strategy == "auto":
            relationship = get_model_relationship(default_model, self.field)
            format_model = relationship.mapper.class_
            if operator is None:
                if relationship.uselist:
                    operator = Operator("any", negate=self.negate)
                else:
                    operator = Operator("has", negate=self.negate)
            value = value.format_for_sqlalchemy(format_model)

        function = operator.function
        arity = operator.arity
        field = Field(default_model, self.field)
        value = self._format_value(field.get_sql_type(), value)

        sqlalchemy_field = field.get_sqlalchemy_field()

        if arity == 1:
            return function(sqlalchemy_field)

        if arity == 2:
            return function(sqlalchemy_field, value)

        raise BadFilterFormatError(f"Operator `{operator}` has unexpected arity {arity}.")


FilterableValueBase = Union[str, int, float, bool, datetime]  # noqa: UP007
FilterableValue = Union[FilterableValueBase, list[FilterableValueBase]]  # noqa: UP007


class BooleanFilter:
    def __init__(self, function, *filters):
        self.function = function
        self.filters = filters

    def get_named_models(self):
        models = set()
        for filter_ in self.filters:
            named_models = filter_.get_named_models()
            if named_models:
                models.update(named_models)
        return models

    def get_join_args(self, model):
        join_args = []
        for filter_ in self.filters:
            field_join_args = filter_.get_join_args(model=model)
            for join_arg in field_join_args:
                if join_arg not in join_args:
                    join_args.append(join_arg)
        return join_args

    def format_for_sqlalchemy(self, default_model):
        return self.function(*[filter_.format_for_sqlalchemy(default_model) for filter_ in self.filters])


def _is_iterable_filter(filter_spec):
    """`filter_spec` may be a list of nested filter specs, or a dict."""
    return isinstance(filter_spec, Iterable) and not isinstance(filter_spec, str | dict)


def init_filters(model, filter_spec):
    if _is_iterable_filter(filter_spec):
        return list(chain.from_iterable(init_filters(model, item) for item in filter_spec))

    if isinstance(filter_spec, dict):
        # Check if filter spec defines a boolean function.
        for boolean_function in BOOLEAN_FUNCTIONS:
            if boolean_function.key in filter_spec:
                # The filter spec is for a boolean-function
                # Get the function argument definitions and validate
                fn_args = filter_spec[boolean_function.key]

                if not _is_iterable_filter(fn_args):
                    raise BadFilterFormatError(
                        f"`{boolean_function.key}` value must be an iterable across the function arguments"
                    )
                if boolean_function.only_one_arg and len(fn_args) != 1:
                    raise BadFilterFormatError(f"`{boolean_function.key}` must have one argument")
                if not boolean_function.only_one_arg and len(fn_args) < 1:
                    raise BadFilterFormatError(f"`{boolean_function.key}` must have one or more arguments")
                return [BooleanFilter(boolean_function.sqlalchemy_fn, *init_filters(model, fn_args))]

    return [Filter(filter_spec)]
