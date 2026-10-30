def wallet_context(request):
    from accounts.models import Role

    staff = request.user.is_authenticated and Role.objects.filter(user=request.user).exists()
    return {
        'has_staff_role': staff,
        'simulation_notice': 'Demonstration money · No cash value',
        'policy_version': 'DWMT-P1',
    }
