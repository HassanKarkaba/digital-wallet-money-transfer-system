class SimulatorRouter:
    """The simulator has its own durable transaction and no cross-store relations."""

    def db_for_read(self, model, **hints):
        return 'simulator' if model._meta.app_label == 'simulator' else None

    def db_for_write(self, model, **hints):
        return 'simulator' if model._meta.app_label == 'simulator' else None

    def allow_relation(self, obj1, obj2, **hints):
        left = obj1._meta.app_label == 'simulator'
        right = obj2._meta.app_label == 'simulator'
        if left or right:
            return left and right
        return None

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        if db == 'simulator':
            return app_label == 'simulator'
        if app_label == 'simulator':
            return False
        return None
