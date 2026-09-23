"""github-proxy: PAT-holding companion service for carter-omp.

carter-omp container holds zero credentials; every GitHub side-effect (REST +
git clone/fetch/push) flows through this service over an HMAC-authenticated
internal channel. See `carter_omp.proxy.server` for the request surface.
"""
