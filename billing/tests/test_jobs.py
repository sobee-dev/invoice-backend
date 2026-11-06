# billing/tests/test_jobs.py
from datetime import timedelta
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from billing.jobs import run_daily_billing_job
from billing.models import Subscription
from billing.plans import BUSINESS, FREE, PLAN_LIMITS, TRIAL, get_effective_plan
from billing.services import handle_paystack_event

from .helpers import make_business, make_owner, set_plan


class TrialTests(TestCase):

    def setUp(self):
        self.business = make_business(make_owner())

    def test_a_new_business_gets_a_15_day_trial_with_business_limits(self):
        plan = get_effective_plan(self.business)
        self.assertEqual(plan.tier, TRIAL)
        self.assertEqual(plan.days_left_in_trial, 15)
        self.assertEqual(plan.limits, PLAN_LIMITS[BUSINESS])

    def test_an_expired_trial_is_free_even_before_the_job_runs(self):
        Subscription.objects.filter(business=self.business).update(trial_ends_at=timezone.now() - timedelta(hours=1))
        self.assertEqual(get_effective_plan(self.business).tier, FREE)

    def test_a_trial_without_an_end_date_is_free(self):
        Subscription.objects.filter(business=self.business).update(trial_ends_at=None)
        self.assertEqual(get_effective_plan(self.business).tier, FREE)

    def test_the_job_moves_an_expired_trial_to_free(self):
        Subscription.objects.filter(business=self.business).update(trial_ends_at=timezone.now() - timedelta(hours=1))
        summary = run_daily_billing_job()
        self.assertEqual(summary['trials_expired'], 1)
        sub = Subscription.objects.get(business=self.business)
        self.assertEqual(sub.plan_tier, 'free')
        self.assertEqual(sub.status, 'active')

    def test_the_job_leaves_a_running_trial_alone(self):
        self.assertEqual(run_daily_billing_job()['trials_expired'], 0)
        self.assertEqual(Subscription.objects.get(business=self.business).plan_tier, 'trial')


@mock.patch('billing.services.emails')
class RenewalAndFailureTests(TestCase):
    """No grace period: a failed payment drops to Free at once, a successful one restores the plan."""

    def setUp(self):
        self.business = make_business(make_owner())
        self.period_end = timezone.now() + timedelta(days=20)
        set_plan(
            self.business, 'business', paystack_subscription_code='SUB_1',
            paystack_customer_code='CUS_1', current_period_end=self.period_end,
        )

    def fail_payment(self):
        handle_paystack_event({'event': 'invoice.payment_failed', 'data': {'subscription': {'subscription_code': 'SUB_1'}}})

    def test_a_failed_payment_drops_to_free_immediately_and_emails_once(self, emails):
        self.fail_payment()
        self.fail_payment()  # Paystack can redeliver
        self.assertEqual(get_effective_plan(self.business).tier, FREE)
        self.assertEqual(Subscription.objects.get(business=self.business).status, 'past_due')
        emails.send_payment_failed_email.assert_called_once()

    def test_a_successful_charge_restores_the_plan(self, emails):
        self.fail_payment()
        new_end = timezone.now() + timedelta(days=30)
        with mock.patch('billing.services._fetch_next_payment_date', return_value=new_end):
            handle_paystack_event({'event': 'charge.success', 'data': {
                'plan': {'plan_code': 'PLN_x'}, 'reference': 'r1',
                'metadata': {'business_id': str(self.business.id)},
                'customer': {'customer_code': 'CUS_1'},
            }})
        sub = Subscription.objects.get(business=self.business)
        self.assertEqual(sub.status, 'active')
        self.assertEqual(sub.current_period_end, new_end)
        self.assertEqual(get_effective_plan(self.business).tier, BUSINESS)

    def test_not_renewing_keeps_the_plan_until_the_period_ends(self, emails):
        handle_paystack_event({'event': 'subscription.not_renew', 'data': {'subscription_code': 'SUB_1'}})
        self.assertTrue(Subscription.objects.get(business=self.business).cancel_at_period_end)
        self.assertEqual(get_effective_plan(self.business).tier, BUSINESS)

        after = self.period_end + timedelta(hours=1)
        self.assertEqual(get_effective_plan(self.business, now=after).tier, FREE)

        summary = run_daily_billing_job(now=after)
        self.assertEqual(summary['plans_downgraded'], 1)
        sub = Subscription.objects.get(business=self.business)
        self.assertEqual((sub.plan_tier, sub.status), ('free', 'active'))
        self.assertIsNone(sub.paystack_subscription_code)

    def test_a_cancelled_subscription_is_emailed_once_and_downgraded_by_the_job(self, emails):
        event = {'event': 'subscription.disable', 'data': {'subscription_code': 'SUB_1'}}
        handle_paystack_event(event)
        handle_paystack_event(event)
        emails.send_subscription_canceled_email.assert_called_once()
        self.assertEqual(get_effective_plan(self.business).tier, FREE)
        self.assertEqual(run_daily_billing_job()['plans_downgraded'], 1)


@override_settings(BILLING_REMINDERS_ENABLED=True)
class ReminderTests(TestCase):

    def setUp(self):
        self.business = make_business(make_owner())
        self.now = timezone.now()

    def set_trial_end(self, delta):
        Subscription.objects.filter(business=self.business).update(trial_ends_at=self.now + delta)

    @mock.patch('billing.jobs.emails.send_trial_ending_soon_email', return_value=True)
    def test_trial_owners_are_reminded_once_a_day_in_the_last_three_days(self, send):
        self.set_trial_end(timedelta(days=2, hours=12))
        run_daily_billing_job(now=self.now)
        run_daily_billing_job(now=self.now)  # same day, no second email
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.args[1], 3)

        run_daily_billing_job(now=self.now + timedelta(days=1))
        self.assertEqual(send.call_count, 2)
        self.assertEqual(send.call_args.args[1], 2)

    @mock.patch('billing.jobs.emails.send_trial_ending_soon_email', return_value=True)
    def test_no_reminder_before_the_last_three_days(self, send):
        self.set_trial_end(timedelta(days=10))
        run_daily_billing_job(now=self.now)
        send.assert_not_called()

    @mock.patch('billing.jobs.emails.send_plan_renewal_soon_email', return_value=True)
    @mock.patch('billing.jobs.emails.send_trial_ending_soon_email', return_value=True)
    def test_subscribing_before_the_end_stops_the_trial_reminders(self, trial_email, renewal_email):
        self.set_trial_end(timedelta(days=2))
        set_plan(self.business, 'business', current_period_end=self.now + timedelta(days=30))
        run_daily_billing_job(now=self.now)
        trial_email.assert_not_called()
        renewal_email.assert_not_called()

    @mock.patch('billing.jobs.emails.send_plan_renewal_soon_email', return_value=True)
    def test_auto_renewing_plans_get_a_renewal_reminder_until_they_renew(self, send):
        set_plan(self.business, 'business', current_period_end=self.now + timedelta(days=2))
        run_daily_billing_job(now=self.now)
        self.assertEqual(send.call_count, 1)

        # The renewal goes through and pushes the period end out of the window.
        Subscription.objects.filter(business=self.business).update(current_period_end=self.now + timedelta(days=32))
        run_daily_billing_job(now=self.now + timedelta(days=1))
        self.assertEqual(send.call_count, 1)

    @mock.patch('billing.jobs.emails.send_plan_ending_soon_email', return_value=True)
    def test_plans_set_not_to_renew_get_the_ending_email(self, send):
        set_plan(
            self.business, 'business', cancel_at_period_end=True,
            current_period_end=self.now + timedelta(days=2),
        )
        run_daily_billing_job(now=self.now)
        send.assert_called_once()

    @mock.patch('billing.jobs.emails.send_trial_ending_soon_email', return_value=False)
    def test_a_failed_send_is_retried_on_the_next_run(self, send):
        self.set_trial_end(timedelta(days=2))
        run_daily_billing_job(now=self.now)
        run_daily_billing_job(now=self.now)
        self.assertEqual(send.call_count, 2)
        self.assertIsNone(Subscription.objects.get(business=self.business).last_reminder_sent_at)

    @override_settings(BILLING_REMINDERS_ENABLED=False)
    @mock.patch('billing.jobs.emails.send_trial_ending_soon_email', return_value=True)
    def test_the_flag_gates_emails_only(self, send):
        Subscription.objects.filter(business=self.business).update(trial_ends_at=self.now - timedelta(hours=1))
        summary = run_daily_billing_job(now=self.now)
        send.assert_not_called()
        self.assertEqual(summary['trials_expired'], 1)