"""Offline-testable selective review policy, not activated in the production Agent.

The feature switch defaults off. Enabling it for an experiment compares both free
providers; paid HTTP still requires GoogleEvaluationGeocoder's separate live gate.
"""
from pyproj import Geod

from .sangis_geocoding import FallbackAddressGeocoder,SanGISAddressGeocoder
from .user_geocoding import AddressResolution,UserAddressGeocoder

GEOD=Geod(ellps='WGS84')
REVIEW_DISTANCE_M=100


def distance_m(a,b):
    return GEOD.inv(a.location.longitude,a.location.latitude,b.location.longitude,b.location.latitude)[2]


def consistent_candidates(result):
    return [c for c in result.candidates if not c.mismatch_reason]


class SelectiveReviewGeocoder:
    def __init__(self,primary=None,sangis=None,google=None,*,enabled=False):
        self.primary=primary or UserAddressGeocoder()
        self.sangis=sangis or SanGISAddressGeocoder()
        self.google=google
        self.enabled=enabled

    def resolve(self,address):
        if not self.enabled:
            return FallbackAddressGeocoder(self.primary,self.sangis).resolve(address)
        census=self.primary.resolve(address);sangis=self.sangis.resolve(address)
        info=dict(provider='selective_review_experiment',free_statuses=dict(census=census.status,sangis=sangis.status))
        a=consistent_candidates(census);b=consistent_candidates(sangis)
        both_unique=census.status==sangis.status=='resolved' and len(a)==len(b)==1
        separation=distance_m(a[0],b[0]) if both_unique else None
        info['free_provider_distance_m']=separation
        if both_unique and separation<=REVIEW_DISTANCE_M:
            census.metadata.update(info,review_reason='free_providers_agree_within_threshold')
            return census
        # One existing, consistent interpolated Census pin remains adequate under
        # the established policy. A property centroid/Review point needs review.
        if census.status=='resolved' and not b:
            census.metadata.update(info,review_reason='only_census_valid_unique')
            return census
        if sangis.status=='resolved' and not a and b[0].precision in {'structure','entrance'}:
            sangis.metadata.update(info,review_reason='only_sangis_address_level_unique')
            return sangis
        reason='free_provider_disagreement' if both_unique else 'no_adequate_unique_free_result'
        info['review_reason']=reason
        pool=a+b
        if self.google is not None:
            google=self.google.resolve(address)
            info['google_status']=google.status
            info['google_diagnostics']=dict(google.metadata)
            g=consistent_candidates(google)
            if google.status=='resolved' and len(g)==1:
                if not pool:
                    google.metadata.update(info)
                    return google
                # A third estimate cannot prove which inconsistent free point is
                # correct. Keep every candidate for explicit user selection.
                pool+=g
            elif google.status=='needs_confirmation':
                pool+=google.candidates
            if not pool and google.status=='provider_error':
                return AddressResolution('provider_error',[],info)
        if pool:
            for candidate in pool:candidate.requires_confirmation=True
            return AddressResolution('needs_confirmation',pool,info)
        if census.status=='provider_error' or sangis.status=='provider_error':
            return AddressResolution('provider_error',[],info)
        status='no_match' if census.status==sangis.status=='no_match' else 'invalid_or_out_of_scope'
        return AddressResolution(status,[],info)
