import itertools

from datetime import datetime, timezone
from deepmerge import always_merger
from marshmallow import Schema, fields, validate
from sqlalchemy.ext.declarative import DeclarativeMeta
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy import Column, inspect, select, types
from sqlalchemy.orm import declared_attr, declarative_base
from sqlalchemy.sql import func
from sqlalchemy.sql.sqltypes import Boolean, Integer, JSON, String
from stringcase import snakecase

from pyvoog.exceptions import ValidationError
from pyvoog.util import Undefined
from pyvoog.validatable import Validatable

class SchemaGenerator:
    skipped_fields = ["id"]

    @classmethod
    def generate_schema(cls, model):

        """ Generate a skeleton Marshmallow schema by reflecting on an
        SQLAlchemy model. Include both columns and virtual attributes. Virtual
        attrs are `allow_none` as not to clash with field validation down the
        pipeline.
        """

        schema_dict = {}

        for c in inspect(model).c:
            schema_dict.update(cls._get_column_schema(c))

        for vattr in cls._get_vattrs(model):
            schema_dict[vattr.name] = fields.Field(allow_none=True)

        return Schema.from_dict(schema_dict)

    @classmethod
    def _get_column_schema(cls, c):
        _type = c.type
        column_schema = {}
        field_kwargs = {}

        if (c.name in cls.skipped_fields or isinstance(c, SchemalessColumn)):
            return column_schema
        elif c.nullable:
            field_kwargs['allow_none'] = True
        elif not c.nullable:
            field_kwargs['required'] = True

        if isinstance(_type, Boolean):
            column_schema[c.name] = fields.Boolean(**field_kwargs)
        elif isinstance(_type, Integer):
            column_schema[c.name] = fields.Integer(**field_kwargs)
        elif isinstance(_type, String):
            validator = None

            if _type.length is not None:
                validator = validate.Length(max=_type.length)

            column_schema[c.name] = fields.Str(validate=validator, **field_kwargs)
        else:
            column_schema[c.name] = fields.Field(**field_kwargs)

        return column_schema

    @classmethod
    def _get_vattrs(cls, model):
        return filter(
            lambda attr: isinstance(attr, VirtualAttribute),
            model.__dict__.values()
        )

class SchemalessColumn(Column):

    """ Represent a JSON database column for holding virtual attributes
    (unstructured data).
    """

    def __init__(self, **kwargs):
        super().__init__(MutableDict.as_mutable(JSON), **kwargs)

class VirtualAttribute(Validatable):

    """ Support virtual attributes, i.e. those not backed by a distinct
    database column on a model. These are routed to a JSON or TEXT field in
    the model's table. The attribute name is automatically deduced by
    ModelMetaClass.
    """

    def __init__(self, default=Undefined, schemaless_field="schemaless"):

        """ Attributes:

        - default - On lookup, return this value instead of raising a KeyError
          if the lookup fails.
        - schemaless_field - Name of the backing JSON or TEXT column.
        """

        super().__init__()

        self.default = default
        self.schemaless_field = schemaless_field

    @property
    def name(self):
        return self.attr_name

    def __get__(self, obj, objtype=None):
        try:
            return getattr(obj, self.schemaless_field)[self.attr_name]
        except (KeyError, TypeError):
            if self.default == Undefined:
                raise
            else:
                return self.default

    def __set__(self, obj, value):
        if obj.schemaless is None:
            obj.schemaless = {}

        obj.schemaless[self.attr_name] = value

    def _set_attr_name(self, attr_name):
        self.attr_name = attr_name

class UTCTimeStamp(types.TypeDecorator):

    """ An SQLAlchemy type decorator for converting and storing all incoming
    timestamps as UTC and returning these as such. Note that SQLite drops
    all time zone information, hence always storing timestamps as UTC is a
    workaround to that issue. If an incoming datetime is naive (i.e.
    contains no time zone information), it is considered to represent time
    in the system timezone. See:

    https://docs.python.org/3/library/datetime.html#datetime.datetime.astimezone
    https://mike.depalatis.net/blog/sqlalchemy-timestamps.html
    """

    impl = types.DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime, dialect):
        return value.astimezone(timezone.utc)

    def process_result_value(self, value, dialect):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)

        return value.astimezone(timezone.utc)

class ModelMetaclass(DeclarativeMeta):

    """ Model base metaclass. """

    def __init__(cls, *args, **kwargs):
        super().__init__(*args, **kwargs)

        cls._set_va_attr_names()
        cls._declare_timestamps()

    def _set_va_attr_names(cls):

        """ Attach attribute names to VirtualAttributes. """

        for k, v in cls.__dict__.items():
            if k.startswith("_") or not isinstance(v, VirtualAttribute):
                continue

            v._set_attr_name(k)

    def _declare_timestamps(cls):
        if not getattr(cls, "include_timestamps", False):
            return

        cls.created_at = Column(UTCTimeStamp(), default=func.now())
        cls.updated_at = Column(UTCTimeStamp(), default=func.now(), onupdate=func.now())

class Model:

    """ A model base class providing the following facilities:

    - The table name is automatically deduced from the class name.
    - A Marshmallow schema is generated based on the model schema. The
      `validate` method checks the object against the schema and any
      constraints installed via Validatable. All errors are merged into a
      single data structure and signalled as a ValidationError. Note that
      ValidatingSession also automatically runs validations before flush on
      new and dirty session members.
    - Default scopes. If a model class has the `default_scope` attribute
      defined, it is expected to be a callable returning a dict of keyword
      attributes to pass to SQLAlchemy's `filter_by`. A statement with the
      scope applied can be retrieved via the `get_scoped_query` method or
      the `scoped_query` property.

    Model attributes may either have a 1:1 mapping to database columns
    (`sqlalchemy.Column` or its subclasses) or be VirtualAttributes which
    are stored in the `schemaless` JSON field. Validations may be attached
    to either.

    TODO: Override default constructor to allow passing vattrs at
    instantiation. See:

    https://docs.sqlalchemy.org/en/14/orm/mapping_styles.html#default-constructor
    """

    id = Column(Integer, primary_key=True)

    @declared_attr
    def __tablename__(cls):
        return snakecase(cls.__name__)

    @classmethod
    def __declare_last__(cls):
        cls.__schema__ = SchemaGenerator.generate_schema(cls)

    @classmethod
    def get_scoped_query(cls, *args):

        """ Return a statement with any default scope defined by the model
        applied. If any arguments are passed, these are forwarded to `select`
        and the table is explicitly specified via `select_from`.
        """

        scope = getattr(cls, "default_scope", None)
        query = select(*args).select_from(cls) if args else select(cls)

        if scope:
            query = query.filter_by(**scope())

        return query

    @classmethod
    @property
    def scoped_query(self):

        """ Property interface to `get_scoped_query`. """

        return self.get_scoped_query()

    def validate(self):
        schema = self.__class__.__schema__()
        attrs = self._get_attr_dict()
        errors = schema.validate(attrs)

        for (attr_name, messages) in self._run_attr_validations():
            always_merger.merge(errors, {attr_name: messages})

        if errors:
            raise ValidationError(errors, None, attrs)

    def as_dict(self):
        return {"id": self.id, **self._get_attr_dict()}

    @property
    def attributes(self):
        raise NotImplementedError()

    @attributes.setter
    def attributes(self, attrs):
        for k, v in attrs.items():
            setattr(self, k, v)

    def _run_attr_validations(self):
        columns = inspect(self.__class__).c
        vattrs = self._get_vattrs()

        for c in itertools.chain(columns, vattrs):
            if isinstance(c, Validatable):
                try:
                    c.is_valid(self)
                except ValidationError as e:
                    yield (c.name, e.messages)

    def _get_vattrs(self):
        return filter(
            lambda attr: isinstance(attr, VirtualAttribute),
            self.__class__.__dict__.values()
        )

    def _get_attr_dict(self):
        schema_fields = self.__class__.__schema__().fields
        return {k: getattr(self, k) for k, v in schema_fields.items()}

Model = declarative_base(cls=Model, metaclass=ModelMetaclass)