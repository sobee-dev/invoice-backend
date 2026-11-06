# billing/tests/test_staff.py
from unittest import mock

from rest_framework.test import APIClient, APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.google import login_gate_response
from billing.plans import staff_access_blocked
from staff.models import Invitation, StaffMember
from staff.views import INVITEE_PLAN_FULL_MESSAGE
from django.contrib.auth import get_user_model

from .helpers import PASSWORD, make_business, make_owner, make_staff, set_plan

User = get_user_model()


class StaffTestBase(APITestCase):
    def setUp(self):
        self.owner = make_owner()
        self.business = make_business(self.owner)

    def login(self, user):
        return APIClient().post(
            '/api/users/login/', {'email': user.email, 'password': PASSWORD}, format='json',
        )


class StaffCreationLimitTests(StaffTestBase):

    def test_free_plan_refuses_new_staff(self):
        set_plan(self.business, 'free')
        self.client.force_authenticate(self.owner)
        with mock.patch('staff.views.send_mail'):
            response = self.client.post(
                '/api/staff/create_staff/',
                {'email': 'new@test.com', 'first_name': 'A', 'last_name': 'B'}, format='json',
            )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data['code'], 'plan_limit_reached')
        self.assertEqual(response.data['resource'], 'staff')
        self.assertEqual(response.data['limit'], 0)
        self.assertFalse(User.objects.filter(email='new@test.com').exists())

    def test_business_plan_allows_two_and_refuses_the_third(self):
        set_plan(self.business, 'business')
        self.client.force_authenticate(self.owner)
        with mock.patch('staff.views.send_mail'):
            for i in range(2):
                response = self.client.post(
                    '/api/staff/create_staff/',
                    {'email': f's{i}@test.com', 'first_name': 'A', 'last_name': 'B'}, format='json',
                )
                self.assertEqual(response.status_code, 201)
            response = self.client.post(
                '/api/staff/create_staff/',
                {'email': 's3@test.com', 'first_name': 'A', 'last_name': 'B'}, format='json',
            )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data['code'], 'plan_limit_reached')

    def test_pending_invitations_hold_no_slot(self):
        set_plan(self.business, 'business')
        self.client.force_authenticate(self.owner)
        with mock.patch('staff.views.send_mail'):
            for i in range(3):
                response = self.client.post('/api/staff/invite/', {'email': f'inv{i}@test.com'}, format='json')
                self.assertEqual(response.status_code, 201)

    def test_invite_is_refused_on_free(self):
        set_plan(self.business, 'free')
        self.client.force_authenticate(self.owner)
        with mock.patch('staff.views.send_mail'):
            response = self.client.post('/api/staff/invite/', {'email': 'inv@test.com'}, format='json')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data['code'], 'plan_limit_reached')

    def test_accepting_an_invitation_is_refused_when_the_seats_are_full(self):
        set_plan(self.business, 'business')
        make_staff(self.business, 'a@test.com')
        make_staff(self.business, 'b@test.com')
        Invitation.objects.create(email='late@test.com', business=self.business, token='tok-late-1')
        response = APIClient().post('/api/staff/accept_invite/', {
            'token': 'tok-late-1', 'password': 'Str0ng-Pass-9', 'password_confirm': 'Str0ng-Pass-9',
            'first_name': 'Late', 'last_name': 'Joiner',
        }, format='json')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data['code'], 'plan_limit_reached')
        self.assertEqual(response.data['error'], INVITEE_PLAN_FULL_MESSAGE)
        self.assertFalse(User.objects.filter(email='late@test.com').exists())

    def test_reactivating_staff_over_the_cap_is_refused(self):
        set_plan(self.business, 'business')
        make_staff(self.business, 'a@test.com')
        make_staff(self.business, 'b@test.com')
        old = make_staff(self.business, 'c@test.com', status='inactive')
        self.client.force_authenticate(self.owner)
        seat = StaffMember.objects.get(user=old)
        response = self.client.post(f'/api/staff/activate/{seat.id}/')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data['code'], 'plan_limit_reached')


class StaffBlockingTests(StaffTestBase):

    def test_only_the_newest_staff_over_the_cap_are_blocked(self):
        set_plan(self.business, 'business')  # cap of 2
        first = make_staff(self.business, 'a@test.com')
        second = make_staff(self.business, 'b@test.com')
        third = make_staff(self.business, 'c@test.com')
        self.assertFalse(staff_access_blocked(first))
        self.assertFalse(staff_access_blocked(second))
        self.assertTrue(staff_access_blocked(third))

        self.assertEqual(self.login(first).status_code, 200)
        blocked = self.login(third)
        self.assertEqual(blocked.status_code, 403)
        self.assertEqual(blocked.data['code'], 'staff_over_plan_limit')

    def test_freeing_a_seat_unblocks_the_newest(self):
        set_plan(self.business, 'business')
        first = make_staff(self.business, 'a@test.com')
        make_staff(self.business, 'b@test.com')
        third = make_staff(self.business, 'c@test.com')
        StaffMember.objects.filter(user=first).update(status='inactive')
        self.assertFalse(staff_access_blocked(third))

    def test_every_staff_member_is_blocked_on_free(self):
        set_plan(self.business, 'free')
        staff = make_staff(self.business, 'a@test.com')
        self.assertTrue(staff_access_blocked(staff))
        response = self.login(staff)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data['code'], 'staff_over_plan_limit')

    def test_an_existing_token_stops_working_after_a_downgrade(self):
        set_plan(self.business, 'business')
        staff = make_staff(self.business, 'a@test.com')
        self.client.force_authenticate(staff)
        self.assertEqual(self.client.get('/api/products/').status_code, 200)
        set_plan(self.business, 'free')
        response = self.client.get('/api/products/')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data['code'], 'staff_over_plan_limit')

    def test_token_refresh_is_refused_for_blocked_staff(self):
        set_plan(self.business, 'free')
        staff = make_staff(self.business, 'a@test.com')
        refresh = RefreshToken.for_user(staff)
        refresh['orig_iat'] = int(__import__('time').time())
        response = APIClient().post('/api/users/token/refresh/', {'refresh': str(refresh)}, format='json')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data['code'], 'staff_over_plan_limit')

    def test_owners_are_never_blocked(self):
        set_plan(self.business, 'free')
        self.assertFalse(staff_access_blocked(self.owner))


class GoogleStaffBlockTests(StaffTestBase):

    def test_a_staff_account_cannot_use_google(self):
        staff = make_staff(self.business, 'a@test.com')
        response = login_gate_response(staff, self.business)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data['code'], 'staff_google_login_not_allowed')

    def test_a_deactivated_staff_account_is_still_blocked(self):
        staff = make_staff(self.business, 'a@test.com', status='inactive')
        self.assertIsNotNone(login_gate_response(staff, self.business))

    def test_owners_can_still_use_google(self):
        self.assertIsNone(login_gate_response(self.owner, self.business))

    def test_the_mobile_google_endpoint_refuses_staff(self):
        staff = make_staff(self.business, 'a@test.com')
        claims = {'email': staff.email, 'given_name': 'A', 'family_name': 'B'}
        with mock.patch('accounts.views.verify_mobile_id_token', return_value=claims):
            response = APIClient().post('/api/users/google_login/', {'id_token': 'x'}, format='json')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data['code'], 'staff_google_login_not_allowed')