# billing/tests/test_intervals.py
from datetime import timedelta
from unittest import mock

import requests
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from billing.jobs import run_daily_billing_job
from billing.models import Subscription
from billing.plans import (
    ANNUAL, BIANNUAL, BUSINESS, ENTERPRISE, FREE, MONTHLY, add_months, get_effective_plan, get_price_ngn,
)
from billing.services import _fetch_next_payment_date, handle_paystack_event

from .helpers import make_business, make_owner, set_plan

PLAN_CODES = {
    'business':   {'monthly': 'PLN_BM', 'biannually': 'PLN_BB', 'annually': 'PLN_BA'},
    'enterprise': {'monthly': 'PLN_EM', 'biannually': 'PLN_EB', 'annually': 'PLN_EA'},
}


def subscription_created(code='SUB_1', plan_code='PLN_BA', interval='annually', next_payment=None):
    data = {
        'subscription_code': code,
        'email_token': 'tok_1',
        'plan': {'plan_code': plan_code, 'interval': interval},
        'customer': {'customer_code': 'CUS_1'},
    }
    if next_payment is not None:
        data['next_payment_date'] = next_payment.isoformat()
    return {'event': 'subscription.create', 'data': data}


@override_settings(PAYSTACK_PLAN_CODES=PLAN_CODES)
@mock.patch('billing.services.emails')
class RecordingTheBillingPeriodTests(TestCase):

    def setUp(self):
        self.business = make_business(make_owner())
        Subscription.objects.filter(business=self.business).update(paystack_customer_code='CUS_1')
        self.now = timezone.now()

    def sub(self):
        return Subscription.objects.get(business=self.business)

    def test_an_annual_payment_records_the_tier_interval_and_paid_through_date(self, emails):
        end = self.now + timedelta(days=365)
        handle_paystack_event(subscription_created(next_payment=end))
        sub = self.sub()
        self.assertEqual((sub.plan_tier, sub.billing_interval, sub.status), ('business', 'annually', 'active'))
        self.assertEqual(sub.current_period_end, end)

    def test_a_biannual_enterprise_payment_is_recorded(self, emails):
        end = self.now + timedelta(days=182)
        handle_paystack_event(subscription_created(plan_code='PLN_EB', interval='biannually', next_payment=end))
        sub = self.sub()
        self.assertEqual((sub.plan_tier, sub.billing_interval), ('enterprise', 'biannually'))

    def test_the_interval_paystack_reports_wins_over_our_mapping(self, emails):
        handle_paystack_event(subscription_created(plan_code='PLN_BM', interval='annually', next_payment=self.now + timedelta(days=365)))
        self.assertEqual(self.sub().billing_interval, 'annually')

    def test_a_missing_next_payment_date_falls_back_to_the_billing_period(self, emails):
        handle_paystack_event(subscription_created())
        end = self.sub().current_period_end
        self.assertIsNotNone(end)
        self.assertGreater(end, self.now + timedelta(days=360))

    def test_activation_clears_the_waiting_marker(self, emails):
        Subscription.objects.filter(business=self.business).update(
            pending_plan_code='PLN_BA', pending_checkout_at=self.now,
        )
        handle_paystack_event(subscription_created(next_payment=self.now + timedelta(days=365)))
        sub = self.sub()
        self.assertIsNone(sub.pending_checkout_at)
        self.assertIsNone(sub.pending_plan_code)


@override_settings(PAYSTACK_PLAN_CODES=PLAN_CODES)
@mock.patch('billing.services.emails')
class LongPeriodPayersAreNeverDemotedEarlyTests(TestCase):

    def setUp(self):
        self.business = make_business(make_owner())
        self.now = timezone.now()
        self.end = self.now + timedelta(days=365)
        set_plan(
            self.business, 'business', billing_interval='annually', paystack_subscription_code='SUB_1',
            paystack_customer_code='CUS_1', current_period_end=self.end,
        )

    def sub(self):
        return Subscription.objects.get(business=self.business)

    def test_an_annual_payer_keeps_the_plan_month_after_month(self, emails):
        for days in (31, 62, 120, 200, 364):
            plan = get_effective_plan(self.business, now=self.now + timedelta(days=days))
            self.assertEqual(plan.tier, BUSINESS, f'demoted after {days} days')

    def test_the_daily_job_leaves_an_annual_payer_alone(self, emails):
        summary = run_daily_billing_job(now=self.now + timedelta(days=40))
        self.assertEqual(summary['plans_downgraded'], 0)
        self.assertEqual(self.sub().plan_tier, 'business')

    def test_cancelling_early_keeps_the_plan_until_the_paid_year_ends(self, emails):
        handle_paystack_event({'event': 'subscription.disable', 'data': {'subscription_code': 'SUB_1'}})
        emails.send_subscription_canceled_email.assert_not_called()
        self.assertTrue(self.sub().cancel_at_period_end)
        self.assertEqual(get_effective_plan(self.business, now=self.now + timedelta(days=100)).tier, BUSINESS)

        after = self.end + timedelta(hours=1)
        self.assertEqual(get_effective_plan(self.business, now=after).tier, FREE)
        self.assertEqual(run_daily_billing_job(now=after)['plans_downgraded'], 1)
        self.assertEqual(self.sub().billing_interval, '')

    def test_disable_after_the_period_has_ended_cancels_and_emails_once(self, emails):
        Subscription.objects.filter(business=self.business).update(current_period_end=self.now - timedelta(hours=2))
        event = {'event': 'subscription.disable', 'data': {'subscription_code': 'SUB_1'}}
        handle_paystack_event(event)
        handle_paystack_event(event)
        emails.send_subscription_canceled_email.assert_called_once()
        self.assertEqual(self.sub().status, 'canceled')

    def test_a_stale_end_date_is_repaired_before_a_cancellation_takes_effect(self, emails):
        # The last renewal's refresh failed, so the stored end is long gone.
        stale = self.now - timedelta(days=30)
        Subscription.objects.filter(business=self.business).update(current_period_end=stale)
        with mock.patch('billing.services._fetch_next_payment_date', return_value=None):
            handle_paystack_event({'event': 'subscription.not_renew', 'data': {'subscription_code': 'SUB_1'}})
        sub = self.sub()
        self.assertTrue(sub.cancel_at_period_end)
        self.assertGreater(sub.current_period_end, self.now)
        self.assertEqual(get_effective_plan(self.business).tier, BUSINESS)

    def test_a_renewal_whose_refresh_failed_still_rolls_forward_one_period(self, emails):
        due = self.now - timedelta(hours=3)
        Subscription.objects.filter(business=self.business).update(current_period_end=due)
        charge = {'event': 'charge.success', 'data': {
            'plan': {'plan_code': 'PLN_BA'}, 'reference': 'r1',
            'metadata': {'business_id': str(self.business.id)}, 'customer': {'customer_code': 'CUS_1'},
        }}
        with mock.patch('billing.services._fetch_next_payment_date', return_value=None):
            handle_paystack_event(charge)
            first = self.sub().current_period_end
            handle_paystack_event(charge)  # Paystack redelivers: must not extend again
        self.assertEqual(first, add_months(due, 12))
        self.assertEqual(self.sub().current_period_end, first)

    def test_a_failed_annual_renewal_drops_to_free_and_a_retry_restores_it(self, emails):
        handle_paystack_event({'event': 'invoice.payment_failed', 'data': {'subscription': {'subscription_code': 'SUB_1'}}})
        self.assertEqual(get_effective_plan(self.business).tier, FREE)
        new_end = self.now + timedelta(days=366)
        with mock.patch('billing.services._fetch_next_payment_date', return_value=new_end):
            handle_paystack_event({'event': 'charge.success', 'data': {
                'plan': {'plan_code': 'PLN_BA'}, 'reference': 'r2',
                'metadata': {'business_id': str(self.business.id)}, 'customer': {'customer_code': 'CUS_1'},
            }})
        self.assertEqual(get_effective_plan(self.business).tier, BUSINESS)
        self.assertEqual(self.sub().current_period_end, new_end)


class PriceAndDateMathTests(TestCase):

    def test_prices_follow_the_monthly_price_and_the_discounts(self):
        self.assertEqual(get_price_ngn(BUSINESS, MONTHLY), 9000)
        self.assertEqual(get_price_ngn(BUSINESS, BIANNUAL), 48600)
        self.assertEqual(get_price_ngn(BUSINESS, ANNUAL), 86400)
        self.assertEqual(get_price_ngn(ENTERPRISE, ANNUAL), 278400)

    def test_adding_months_clamps_to_the_end_of_a_short_month(self):
        start = timezone.now().replace(year=2027, month=1, day=31)
        self.assertEqual(add_months(start, 1).day, 28)
        self.assertEqual(add_months(start, 12).year, 2028)


@override_settings(PAYSTACK_PLAN_CODES=PLAN_CODES, BILLING_SUCCESS_URL='https://app.billbuzz.ng/billing/success')
class CheckoutFlowTests(APITestCase):

    def setUp(self):
        self.owner = make_owner()
        self.business = make_business(self.owner)
        self.client.force_authenticate(self.owner)

    def checkout(self, **body):
        return self.client.post('/api/billing/checkout/', body, format='json')

    def test_the_plans_page_lists_every_billing_period_with_prices(self):
        response = self.client.get('/api/billing/plans/')
        self.assertEqual(response.status_code, 200)
        business_plan = next(p for p in response.data['plans'] if p['tier'] == 'business')
        self.assertEqual([i['interval'] for i in business_plan['intervals']], ['monthly', 'biannually', 'annually'])
        annual = business_plan['intervals'][2]
        self.assertEqual(annual['price_ngn'], 86400)
        self.assertEqual(annual['saving_ngn'], 9000 * 12 - 86400)
        self.assertTrue(annual['checkout_available'])
        free_plan = next(p for p in response.data['plans'] if p['tier'] == 'free')
        self.assertEqual(free_plan['intervals'], [])

    @override_settings(PAYSTACK_PLAN_CODES={'business': {'monthly': 'PLN_BM', 'biannually': '', 'annually': ''},
                                            'enterprise': {'monthly': '', 'biannually': '', 'annually': ''}})
    def test_an_unconfigured_period_is_marked_unavailable_and_refused(self):
        plans = self.client.get('/api/billing/plans/').data['plans']
        business_plan = next(p for p in plans if p['tier'] == 'business')
        self.assertFalse(business_plan['intervals'][2]['checkout_available'])
        response = self.checkout(tier='business', interval='annually')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['code'], 'plan_unavailable')

    def test_unknown_tier_and_interval_are_refused(self):
        self.assertEqual(self.checkout(tier='gold', interval='monthly').data['code'], 'unknown_plan')
        self.assertEqual(self.checkout(tier='business', interval='weekly').data['code'], 'unknown_interval')

    def test_staff_cannot_start_a_checkout(self):
        from .helpers import make_staff
        staff = make_staff(self.business, 's@test.com')
        set_plan(self.business, 'business')
        self.client.force_authenticate(staff)
        self.assertEqual(self.checkout(tier='business', interval='annually').status_code, 403)

    def test_a_valid_checkout_returns_the_url_and_reference_with_the_chosen_period(self):
        with mock.patch('billing.services._paystack_request') as call:
            call.side_effect = [
                {'customer_code': 'CUS_9'},
                {'authorization_url': 'https://checkout.paystack.com/abc', 'reference': 'ref_123456'},
            ]
            response = self.checkout(tier='enterprise', interval='biannually')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {'url': 'https://checkout.paystack.com/abc', 'reference': 'ref_123456'})
        sent = call.call_args_list[1].kwargs['json']
        self.assertEqual(sent['plan'], 'PLN_EB')
        self.assertEqual(sent['amount'], 156600 * 100)
        self.assertEqual(sent['metadata']['interval'], 'biannually')
        self.assertEqual(sent['callback_url'], 'https://app.billbuzz.ng/billing/success')

    def test_a_second_checkout_is_refused_while_a_payment_is_waiting_to_activate(self):
        Subscription.objects.filter(business=self.business).update(
            pending_plan_code='PLN_BA', pending_checkout_at=timezone.now(),
        )
        response = self.checkout(tier='business', interval='annually')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data['code'], 'checkout_in_progress')

    def test_an_old_unactivated_marker_does_not_lock_the_owner_out(self):
        Subscription.objects.filter(business=self.business).update(
            pending_plan_code='PLN_BA', pending_checkout_at=timezone.now() - timedelta(days=2),
        )
        with mock.patch('billing.views.create_subscription_checkout', return_value={'url': 'u', 'reference': 'r'}):
            self.assertEqual(self.checkout(tier='business', interval='annually').status_code, 200)

    def test_an_active_subscriber_is_told_when_it_renews_instead_of_paying_twice(self):
        end = timezone.now() + timedelta(days=300)
        set_plan(self.business, 'business', billing_interval='annually',
                 paystack_subscription_code='SUB_1', current_period_end=end)
        response = self.checkout(tier='enterprise', interval='annually')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data['code'], 'already_subscribed')
        self.assertEqual(response.data['billing_interval'], 'annually')
        self.assertEqual(response.data['plan'], 'business')

    def test_someone_who_cancelled_can_subscribe_again(self):
        set_plan(self.business, 'business', billing_interval='annually', paystack_subscription_code='SUB_1',
                 cancel_at_period_end=True, current_period_end=timezone.now() + timedelta(days=30))
        with mock.patch('billing.views.create_subscription_checkout', return_value={'url': 'u', 'reference': 'r'}):
            self.assertEqual(self.checkout(tier='business', interval='annually').status_code, 200)


@override_settings(PAYSTACK_PLAN_CODES=PLAN_CODES)
class ConfirmCheckoutTests(APITestCase):

    def setUp(self):
        self.owner = make_owner()
        self.business = make_business(self.owner)
        self.client.force_authenticate(self.owner)

    def confirm(self, reference='ref_123456'):
        return self.client.post('/api/billing/checkout/confirm/', {'reference': reference}, format='json')

    def verified(self, **overrides):
        data = {'status': 'success', 'metadata': {
            'business_id': str(self.business.id), 'tier': 'business', 'interval': 'annually'}}
        data.update(overrides)
        return mock.patch('billing.services._paystack_request', return_value=data)

    def test_a_verified_payment_marks_the_checkout_pending_and_reports_it(self):
        with self.verified():
            response = self.confirm()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['checkout_pending'])
        self.assertEqual(response.data['plan'], 'trial')  # the webhook has not activated it yet
        sub = Subscription.objects.get(business=self.business)
        self.assertEqual(sub.pending_plan_code, 'PLN_BA')
        self.assertTrue(self.client.get('/api/billing/status/').data['checkout_pending'])

    def test_a_payment_that_did_not_succeed_is_not_accepted(self):
        with self.verified(status='abandoned'):
            response = self.confirm()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['code'], 'payment_not_confirmed')
        self.assertIsNone(Subscription.objects.get(business=self.business).pending_checkout_at)

    def test_a_payment_for_another_business_is_not_accepted(self):
        with self.verified(metadata={'business_id': 'someone-else', 'tier': 'business', 'interval': 'annually'}):
            response = self.confirm()
        self.assertEqual(response.status_code, 400)

    def test_a_malformed_reference_never_reaches_paystack(self):
        with mock.patch('billing.services._paystack_request') as call:
            response = self.confirm(reference='../../customer')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['code'], 'invalid_reference')
        call.assert_not_called()

    def test_confirming_after_activation_does_not_mark_it_pending_again(self):
        set_plan(self.business, 'business', plan_code='PLN_BA', billing_interval='annually')
        with self.verified():
            response = self.confirm()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data['checkout_pending'])

    def test_paystack_being_down_gives_a_retryable_error(self):
        with mock.patch('billing.services._paystack_request', side_effect=requests.ConnectionError()):
            response = self.confirm()
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.data['code'], 'billing_unavailable')

    def test_the_status_endpoint_reports_the_billing_period_once_active(self):
        set_plan(self.business, 'business', billing_interval='biannually',
                 current_period_end=timezone.now() + timedelta(days=170))
        data = self.client.get('/api/billing/status/').data
        self.assertEqual(data['plan'], 'business')
        self.assertEqual(data['billing_interval'], 'biannually')
        self.assertEqual(data['billing_interval_label'], 'Every 6 months')
        self.assertFalse(data['checkout_pending'])


class StuckCheckoutTests(TestCase):

    def test_the_job_reports_a_paid_checkout_that_never_activated(self):
        business = make_business(make_owner())
        Subscription.objects.filter(business=business).update(
            pending_plan_code='PLN_BA', pending_checkout_at=timezone.now() - timedelta(hours=1),
        )
        with self.assertLogs('billing.jobs', level='ERROR'):
            summary = run_daily_billing_job()
        self.assertEqual(summary['stuck_checkouts'], 1)

    def test_a_fresh_pending_checkout_is_not_reported(self):
        business = make_business(make_owner())
        Subscription.objects.filter(business=business).update(
            pending_plan_code='PLN_BA', pending_checkout_at=timezone.now(),
        )
        self.assertEqual(run_daily_billing_job()['stuck_checkouts'], 0)


class FetchFailureTests(TestCase):

    def test_a_failed_period_lookup_returns_none_instead_of_failing_the_webhook(self):
        with mock.patch('billing.services._paystack_request', side_effect=requests.ConnectionError()):
            self.assertIsNone(_fetch_next_payment_date('SUB_1'))