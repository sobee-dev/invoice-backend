# billing/tests/test_templates.py
from datetime import timedelta

from django.utils import timezone
from rest_framework.test import APITestCase

from billing.jobs import reset_locked_templates
from business.models import Business, SyncStatus

from .helpers import make_business, make_owner, set_plan


class TemplateRuleTests(APITestCase):

    def setUp(self):
        self.owner = make_owner()
        self.business = make_business(self.owner)
        self.client.force_authenticate(self.owner)

    def patch_template(self, template_id):
        return self.client.patch('/api/businesses/me/', {'selected_template_id': template_id}, format='json')

    def test_a_new_business_defaults_to_classic(self):
        self.assertEqual(self.business.selected_template_id, 'classic')

    def test_the_trial_may_use_every_template(self):
        self.assertEqual(self.patch_template('modern').status_code, 200)

    def test_free_may_not_pick_another_template(self):
        set_plan(self.business, 'free')
        response = self.patch_template('modern')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data['code'], 'plan_feature_locked')
        self.assertEqual(response.data['feature'], 'templates')
        self.assertEqual(response.data['allowed'], ['classic'])
        self.business.refresh_from_db()
        self.assertEqual(self.business.selected_template_id, 'classic')

    def test_free_may_keep_the_default(self):
        set_plan(self.business, 'free')
        self.assertEqual(self.patch_template('classic').status_code, 200)

    def test_an_unknown_template_id_is_a_plain_validation_error(self):
        self.assertEqual(self.patch_template('fancy').status_code, 400)

    def test_onboarding_cannot_smuggle_a_premium_template_onto_free(self):
        set_plan(self.business, 'free')
        response = self.client.post(
            f'/api/businesses/{self.business.pk}/complete-onboarding/',
            {'selected_template_id': 'professional'}, format='json',
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data['code'], 'plan_feature_locked')

    def test_the_daily_job_resets_a_premium_template_after_downgrade(self):
        Business.objects.filter(pk=self.business.pk).update(
            selected_template_id='modern', sync_status=SyncStatus.SYNCED,
        )
        set_plan(self.business, 'free')
        self.assertEqual(reset_locked_templates(timezone.now()), 1)
        self.business.refresh_from_db()
        self.assertEqual(self.business.selected_template_id, 'classic')
        self.assertEqual(self.business.sync_status, SyncStatus.PENDING)

    def test_the_daily_job_leaves_paid_businesses_alone(self):
        Business.objects.filter(pk=self.business.pk).update(selected_template_id='modern')
        set_plan(self.business, 'business', current_period_end=timezone.now() + timedelta(days=20))
        self.assertEqual(reset_locked_templates(timezone.now()), 0)