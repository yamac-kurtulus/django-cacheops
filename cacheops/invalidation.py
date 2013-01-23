# -*- coding: utf-8 -*-
from redis.exceptions import WatchError

from cacheops.conf import redis_client
from cacheops.utils import get_model_name


__all__ = ('invalidate_obj', 'invalidate_model', 'invalidate_all')


def serialize_scheme(scheme):
    return ','.join(scheme)

def deserialize_scheme(scheme):
    return tuple(scheme.split(','))

def conj_cache_key(model, conj):
    return 'conj:%s:' % get_model_name(model) + '&'.join('%s=%s' % t for t in sorted(conj))

def conj_cache_key_from_scheme(model, scheme, values):
    return 'conj:%s:' % get_model_name(model) + '&'.join('%s=%s' % (f, values[f]) for f in scheme)


class ConjSchemes(object):
    """
    A container for managing models scheme collections.
    Schemes are stored in redis and cached locally.
    """
    def __init__(self):
        self.local = {}
        self.versions = {}

    def get_lookup_key(self, model_or_name):
        if not isinstance(model_or_name, basestring):
            model_or_name = get_model_name(model_or_name)
        return 'schemes:%s' % model_or_name

    def get_version_key(self, model_or_name):
        if not isinstance(model_or_name, basestring):
            model_or_name = get_model_name(model_or_name)
        return 'schemes:%s:version' % model_or_name

    def load_schemes(self, model):
        model_name = get_model_name(model)

        txn = redis_client.pipeline()
        txn.get(self.get_version_key(model))
        txn.smembers(self.get_lookup_key(model_name))
        version, members = txn.execute()

        self.local[model_name] = set(map(deserialize_scheme, members))
        self.local[model_name].add(()) # Всегда добавляем пустую схему
        self.versions[model_name] = int(version or 0)
        return self.local[model_name]

    def schemes(self, model):
        model_name = get_model_name(model)
        try:
            return self.local[model_name]
        except KeyError:
            return self.load_schemes(model)

    def version(self, model):
        try:
            return self.versions[get_model_name(model)]
        except KeyError:
            return 0

    def ensure_known(self, model, new_schemes):
        """
        Ensure that `new_schemes` are known or know them
        """
        new_schemes = set(new_schemes)
        model_name = get_model_name(model)
        loaded = False

        if model_name not in self.local:
            self.load_schemes(model)
            loaded = True
        schemes = self.local[model_name]

        if new_schemes - schemes:
            if not loaded:
                schemes = self.load_schemes(model)
            if new_schemes - schemes:
                # Write new schemes to redis
                txn = redis_client.pipeline()
                txn.incr(self.get_version_key(model_name)) # Увеличиваем версию схем

                lookup_key = self.get_lookup_key(model_name)
                for scheme in new_schemes - schemes:
                    txn.sadd(lookup_key, serialize_scheme(scheme))
                txn.execute()

                # Updating local version
                self.local[model_name].update(new_schemes)
                # We increment here instead of using incr result from redis,
                # because even our updated collection could be already obsolete
                self.versions[model_name] += 1

    def clear(self, model):
        """
        Clears schemes for models
        """
        redis_client.delete(self.get_lookup_key(model))
        redis_client.incr(self.get_version_key(model))

    def clear_all(self):
        self.local = {}
        for model_name in self.versions:
            self.versions[model_name] += 1


cache_schemes = ConjSchemes()


def invalidate_from_dict(model, values):
    """
    Invalidates caches that can possibly be influenced by object
    """

    # Computing version key string from model and application names
    version_key = cache_schemes.get_version_key(model)

    # Loading model schemes from local memory (or from redis)
    schemes = cache_schemes.schemes(model)

    try_count = 3
    while try_count > 0:
        try_count -= 1

        # Create a list of invalidators from list of schemes and values of object fields
        conjs_keys = [conj_cache_key_from_scheme(model, scheme, values) for scheme in schemes]

        # Optimistic locking: we hope schemes version not changes while we delete cache keys.
        # All invalidators are removed in Redis transaction, so no cache key could be added
        # in the middle, and no cache key could hang with it's invalidator removed
        def _invalidate_conjs(pipe):

            # Starting MULTI block

            pipe.multi()
            # Check if our version of schemes for model is obsolete, update them and redo if needed
            # This shouldn't be happen too often once schemes are filled a bit
            pipe.get(version_key)
            # Get a union of all cache keys registered in invalidators
            pipe.sunion(conjs_keys)
            # `conjs_keys` are keys of sets containing `cache_keys` we are going to delete,
            # so we'll remove them too.
            # NOTE: There could be some other invalidators not matched with current object,
            #       which reference cache keys we delete, they will be hanging out for a while.
            pipe.delete(*conjs_keys)

        version, cache_keys, _ = redis_client.transaction(_invalidate_conjs)
        # Next two lines are the only where python process crash could lead to
        # cache keys hang without invalidators.
        # But it's better than infinite loop caused by numerous WatchErrors.
        if cache_keys:
            redis_client.delete(*cache_keys)

        # OK, we invalidated all conjunctions we had in memory, but model schema
        # may have been changed in redis long time ago. If this happened,
        # schema version will not match and we should load new schemes,
        # compute
        if int(version or 0) == cache_schemes.version(model):
            # schemes version is OK, so invalidation completed
            return
        # Updating schemes with new values from redis.
        # Hope, this happens rarely :)
        schemes = cache_schemes.load_schemes(model)

    # if number of tries exceeds N, we should not go to infinite loop,
    # raise RuntimeError instead.
    raise RuntimeError("can't invalidate dict")


def invalidate_obj(obj):
    """
    Invalidates caches that can possibly be influenced by object
    """
    invalidate_from_dict(obj.__class__, obj.__dict__)


def invalidate_model(model):
    """
    Invalidates all caches for given model.
    NOTE: This is a heavy artilery which uses redis KEYS request,
          which could be relatively slow on large datasets.
    """
    conjs_keys = redis_client.keys('conj:%s:*' % get_model_name(model))
    if isinstance(conjs_keys, str):
        conjs_keys = conjs_keys.split()

    if conjs_keys:
        cache_keys = redis_client.sunion(conjs_keys)
        redis_client.delete(*(list(cache_keys) + conjs_keys))

    # BUG: a race bug here, ignoring since invalidate_model() is not for hot production use
    cache_schemes.clear(model)

def invalidate_all():
    redis_client.flushdb()
    cache_schemes.clear_all()
