from django.conf import settings
from django.template import (Library, Node, Variable,
    TOKEN_BLOCK, TOKEN_COMMENT, TOKEN_TEXT, TOKEN_VAR,
    TemplateSyntaxError, VariableDoesNotExist, Context)
from django.utils.encoding import smart_str
from django.templatetags.cache import CacheNode

from phased.utils import pickle_context, flatten_context, backup_csrf_token, second_pass_render

register = Library()


def parse(parser):
    """
    Parse to the end of a phased block. This is different than Parser.parse()
    in that it does not generate Node objects; it simply yields tokens.
    """
    depth = 0
    while parser.tokens:
        token = parser.next_token()
        if token.token_type == TOKEN_BLOCK:
            if token.contents == 'phased':
                depth += 1
            elif token.contents == 'endphased':
                depth -= 1
        if depth < 0:
            break
        yield token
    if not parser.tokens and depth >= 0:
        parser.unclosed_block_tag(('endphased',))


class PhasedNode(Node):
    """
    Generated by {% phased %} tag. Outputs the literal content of the phased
    block with pickled context, enclosed in a delimited block that can be
    parsed by the second pass rendering middleware.
    """
    def __init__(self, content, var_names):
        self.var_names = var_names
        self.content = content

    def __repr__(self):
        return "<Phased Node: '%s'>" % smart_str(self.content[:25], 'ascii',
                errors='replace')

    def render(self, context):
        # our main context
        storage = Context()

        # stash the whole context if needed
        if getattr(settings, 'PHASED_KEEP_CONTEXT', False):
            storage.update(flatten_context(context))

        # but check if there are variables specifically wanted
        for var_name in self.var_names:
            if var_name[0] in ('"', "'") and var_name[-1] == var_name[0]:
                var_name = var_name[1:-1]
            try:
                storage[var_name] = Variable(var_name).resolve(context)
            except VariableDoesNotExist:
                raise TemplateSyntaxError(
                    '"phased" tag got an unknown variable: %r' % var_name)

        storage = backup_csrf_token(context, storage)

        # lastly return the pre phased template part
        return u'%(delimiter)s%(content)s%(pickled)s%(delimiter)s' % {
            'content': self.content,
            'delimiter': settings.PHASED_SECRET_DELIMITER,
            'pickled': pickle_context(storage),
        }


@register.tag
def phased(parser, token):
    """
    Template tag to denote a template section to render a second time via
    a middleware.

    Usage::

        {% load phased_tags %}
        {% phased with [var1] [var2] .. %}
            .. some content to be rendered a second time ..
        {% endphased %}

    You can pass it a list of context variable names to automatically
    save those variables for the second pass rendering of the template,
    e.g.::

        {% load phased_tags %}
        {% phased with comment_count object %}
            There are {{ comment_count }} comments for "{{ object }}".
        {% endphased %}

    Alternatively you can also set the ``PHASED_KEEP_CONTEXT`` setting to
    ``True`` to automatically keep the whole context for each phased block.

    Note: Lazy objects such as messages and csrf tokens aren't kept.

    """
    literal = ''.join({
        TOKEN_BLOCK: '{%% %s %%}',
        TOKEN_VAR: '{{ %s }}',
        TOKEN_COMMENT: '{# %s #}',
        TOKEN_TEXT: '%s',
    }[token.token_type] % token.contents for token in parse(parser))
    tokens = token.contents.split()
    if len(tokens) > 1 and tokens[1] != 'with':
        raise TemplateSyntaxError(u"'%r' tag requires the second argument to be 'with'." % tokens[0])
        if len(tokens) == 2:
            raise TemplateSyntaxError(u"'%r' tag requires at least one context variable name." % tokens[0])
    return PhasedNode(literal, tokens[2:])


class PhasedCacheNode(CacheNode):
    def render(self, context):
        """
        Template tag that acts like Django's cached tag
        except that it does a second pass rendering.

        Requires `RequestContext` and
        `django.core.context_processors.request` to be in
        TEMPLATE_CONTEXT_PROCESSORS
        """
        content = super(PhasedCacheNode, self).render(context)
        return second_pass_render(context['request'], content)


@register.tag('phasedcache')
def do_cache(parser, token):
    """
    Taken from django.templatetags.cache and changed ending tag.

    This will cache the contents of a template fragment for a given amount
    of time and do a second pass render on the contents

    Usage::

        {% load phased_tags %}
        {% phasedcache [expire_time] [fragment_name] %}
            .. some expensive processing ..
        {% endphasedcache %}

    This tag also supports varying by a list of arguments::

        {% load phased_tags %}
        {% phasedcache [expire_time] [fragment_name] [var1] [var2] .. %}
            .. some expensive processing ..
        {% endphasedcache %}

    Each unique set of arguments will result in a unique cache entry.
    """
    nodelist = parser.parse(('endphasedcache',))
    parser.delete_first_token()
    tokens = token.contents.split()
    if len(tokens) < 3:
        raise TemplateSyntaxError(u"'%r' tag requires at least 2 arguments." % tokens[0])
    return PhasedCacheNode(nodelist, tokens[1], tokens[2], tokens[3:])
