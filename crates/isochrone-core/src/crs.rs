//! RD New (EPSG:28992) ↔ WGS84 (EPSG:4326) transform.
//!
//! Hand-coded simplified RDNAPTRANS (Bessel 1841 + 7-param Helmert + oblique
//! stereographic). Verified against pyproj `Transformer.from_crs(...)` to
//! sub-millimeter on points across NL — no PROJ/GEOS native dependency.

use std::sync::OnceLock;

// Bessel 1841 ellipsoid.
const A: f64 = 6377397.155;
const INV_F: f64 = 299.152_812_8;
const F: f64 = 1.0 / INV_F;
const ES: f64 = F * (2.0 - F); // e^2

// WGS84 (GRS80) ellipsoid.
const WGS84_A: f64 = 6_378_137.0;
const WGS84_ES: f64 = 0.006_694_379_990_14;

// Oblique stereographic projection parameters.
const PHI0_DEG: f64 = 52.156_160_555_555_56;
const LAM0_DEG: f64 = 5.387_638_888_888_89;
const K0: f64 = 0.999_907_9;
const FE: f64 = 155_000.0;
const FN: f64 = 463_000.0;

// 7-param Helmert Bessel -> WGS84 (position vector / `+towgs84`).
const DX: f64 = 565.4171;
const DY: f64 = 50.3319;
const DZ: f64 = 465.5524;
const ARCSEC_TO_RAD: f64 = std::f64::consts::PI / (180.0 * 3600.0);
const RX: f64 = -0.398_957 * ARCSEC_TO_RAD;
const RY: f64 = 0.343_988 * ARCSEC_TO_RAD;
const RZ: f64 = -1.877_40 * ARCSEC_TO_RAD;
const SCALE: f64 = 4.0725e-6;

/// Derived projection constants (computed once).
struct Proj {
    e: f64,
    r: f64,
    n: f64,
    c: f64,
    chi0: f64,
    sin_chi0: f64,
    cos_chi0: f64,
}

fn proj() -> &'static Proj {
    static P: OnceLock<Proj> = OnceLock::new();
    P.get_or_init(|| {
        let e = ES.sqrt();
        let phi0 = PHI0_DEG.to_radians();
        let nu0 = A / (1.0 - ES * phi0.sin().powi(2)).sqrt();
        let rho0 = A * (1.0 - ES) / (1.0 - ES * phi0.sin().powi(2)).powf(1.5);
        let r = (rho0 * nu0).sqrt();
        let n = (1.0 + ES * phi0.cos().powi(4) / (1.0 - ES)).sqrt();
        let s1 = (1.0 + phi0.sin()) / (1.0 - phi0.sin());
        let s2 = (1.0 - e * phi0.sin()) / (1.0 + e * phi0.sin());
        let w1 = (s1 * s2.powf(e)).powf(n);
        let sin_chi0_approx = (w1 - 1.0) / (w1 + 1.0);
        let c = (n + phi0.sin()) * (1.0 - sin_chi0_approx)
            / ((n - phi0.sin()) * (1.0 + sin_chi0_approx));
        let w2 = c * w1;
        let chi0 = ((w2 - 1.0) / (w2 + 1.0)).asin();
        Proj {
            e,
            r,
            n,
            c,
            chi0,
            sin_chi0: chi0.sin(),
            cos_chi0: chi0.cos(),
        }
    })
}

fn geod_to_geoc(phi: f64, lam: f64, a: f64, es: f64) -> (f64, f64, f64) {
    let n = a / (1.0 - es * phi.sin().powi(2)).sqrt();
    let x = n * phi.cos() * lam.cos();
    let y = n * phi.cos() * lam.sin();
    let z = n * (1.0 - es) * phi.sin();
    (x, y, z)
}

fn geoc_to_geod(x: f64, y: f64, z: f64, a: f64, es: f64) -> (f64, f64) {
    let b = a * (1.0 - es).sqrt();
    let ep = (a * a - b * b) / (b * b);
    let p = (x * x + y * y).sqrt();
    let th = (z * a).atan2(p * b);
    let phi = (z + ep * b * th.sin().powi(3)).atan2(p - es * a * th.cos().powi(3));
    let lam = y.atan2(x);
    (phi, lam)
}

fn helmert_bessel_to_wgs84(x: f64, y: f64, z: f64) -> (f64, f64, f64) {
    let m = 1.0 + SCALE;
    (
        DX + m * (x - RZ * y + RY * z),
        DY + m * (RZ * x + y - RX * z),
        DZ + m * (-RY * x + RX * y + z),
    )
}

fn helmert_wgs84_to_bessel(x: f64, y: f64, z: f64) -> (f64, f64, f64) {
    let x1 = x - DX;
    let y1 = y - DY;
    let z1 = z - DZ;
    let m = 1.0 / (1.0 + SCALE);
    (
        m * (x1 + RZ * y1 - RY * z1),
        m * (-RZ * x1 + y1 + RX * z1),
        m * (RY * x1 - RX * y1 + z1),
    )
}

fn stereo_forward(phi: f64, lam: f64, p: &Proj) -> (f64, f64) {
    let lam0 = LAM0_DEG.to_radians();
    let s_phi = phi.sin();
    let s = (1.0 + s_phi) / (1.0 - s_phi);
    let sp = (1.0 - p.e * s_phi) / (1.0 + p.e * s_phi);
    let w = p.c * (s * sp.powf(p.e)).powf(p.n);
    let chi = ((w - 1.0) / (w + 1.0)).asin();
    let lam_c = p.n * (lam - lam0) + lam0;
    let dlam = lam_c - lam0;
    let b = 1.0 + chi.sin() * p.sin_chi0 + chi.cos() * p.cos_chi0 * dlam.cos();
    let e = FE + 2.0 * p.r * K0 * chi.cos() * dlam.sin() / b;
    let n = FN
        + 2.0 * p.r * K0
            * (chi.sin() * p.cos_chi0 - chi.cos() * p.sin_chi0 * dlam.cos())
            / b;
    (e, n)
}

fn conformal_to_geod(chi: f64, p: &Proj) -> f64 {
    let w = (1.0 + chi.sin()) / (1.0 - chi.sin());
    let target = (w / p.c).powf(1.0 / p.n); // = S * Sp^e
    let mut phi = chi;
    for _ in 0..20 {
        let sp = phi.sin();
        let s = (1.0 + sp) / (1.0 - sp);
        let sp_ = (1.0 - p.e * sp) / (1.0 + p.e * sp);
        let val = s * sp_.powf(p.e);
        let ds = 2.0 * phi.cos() / (1.0 - sp).powi(2);
        let dsp = -2.0 * p.e * phi.cos() / (1.0 + p.e * sp).powi(2);
        let dval = sp_.powf(p.e) * ds + s * p.e * sp_.powf(p.e - 1.0) * dsp;
        phi -= (val - target) / dval;
    }
    phi
}

fn stereo_inverse(x: f64, y: f64, p: &Proj) -> (f64, f64) {
    let lam0 = LAM0_DEG.to_radians();
    let de = x - FE;
    let dn = y - FN;
    let rho = (de * de + dn * dn).sqrt();
    let (chi, lam_c) = if rho < 1e-9 {
        (p.chi0, lam0)
    } else {
        let cc = 2.0 * (rho / (2.0 * p.r * K0)).atan();
        let sin_chi = (p.sin_chi0 * cc.cos() + p.cos_chi0 * cc.sin() * (dn / rho))
            .clamp(-1.0, 1.0);
        let chi = sin_chi.asin();
        let cos_chi = chi.cos();
        let cos_dl = (cc.cos() * p.cos_chi0 - p.sin_chi0 * cc.sin() * (dn / rho)) / cos_chi;
        let sin_dl = (de / rho) * cc.sin() / cos_chi;
        (chi, lam0 + sin_dl.atan2(cos_dl))
    };
    let phi = conformal_to_geod(chi, p);
    let lam = lam0 + (lam_c - lam0) / p.n;
    (phi, lam)
}

/// WGS84 (lon, lat) → RD New (x, y), metres. Matches `_TO_RD.transform`.
#[must_use]
pub fn to_rd(lon: f64, lat: f64) -> (f64, f64) {
    let phi = lat.to_radians();
    let lam = lon.to_radians();
    let (x, y, z) = geod_to_geoc(phi, lam, WGS84_A, WGS84_ES);
    let (x, y, z) = helmert_wgs84_to_bessel(x, y, z);
    let (phi_b, lam_b) = geoc_to_geod(x, y, z, A, ES);
    stereo_forward(phi_b, lam_b, proj())
}

/// RD New (x, y) → WGS84 (lon, lat). Matches `_TO_WGS84.transform`.
#[must_use]
pub fn to_wgs84(x: f64, y: f64) -> (f64, f64) {
    let (phi_b, lam_b) = stereo_inverse(x, y, proj());
    let (x, y, z) = geod_to_geoc(phi_b, lam_b, A, ES);
    let (x, y, z) = helmert_bessel_to_wgs84(x, y, z);
    let (phi_w, lam_w) = geoc_to_geod(x, y, z, WGS84_A, WGS84_ES);
    (lam_w.to_degrees(), phi_w.to_degrees())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn close(a: (f64, f64), b: (f64, f64), tol_m: f64) -> bool {
        // lon error ~111320 m/deg * cos(lat); lat ~110574 m/deg
        let dx = (a.0 - b.0).abs() * 111_320.0 * b.1.to_radians().cos();
        let dy = (a.1 - b.1).abs() * 110_574.0;
        (dx * dx + dy * dy).sqrt() < tol_m
    }

    #[test]
    fn forward_matches_pyproj() {
        // (lon, lat) -> (x, y) from pyproj always_xy=True
        let cases: [((f64, f64), (f64, f64)); 6] = [
            ((4.892, 52.373), (121_276.818_648, 487_351.243_320)),
            ((5.121, 52.09), (136_756.027_174, 455_782.358_715)),
            ((5.198, 52.066), (142_026.181_193, 453_095.608_231)),
            ((6.0, 52.5), (196_611.368_819, 501_543.123_911)),
            ((3.5, 51.5), (23_969.303_356, 391_804.958_187)),
            ((7.0, 53.2), (262_770.290_399, 580_468.022_366)),
        ];
        for ((lon, lat), (ex, ey)) in cases {
            let (x, y) = to_rd(lon, lat);
            assert!((x - ex).abs() < 0.01, "x {x} vs {ex}");
            assert!((y - ey).abs() < 0.01, "y {y} vs {ey}");
        }
    }

    #[test]
    fn inverse_matches_pyproj() {
        let cases: [((f64, f64), (f64, f64)); 6] = [
            ((121_276.819, 487_351.243), (4.892_000_006, 52.373_000_001)),
            ((136_756.027, 455_782.359), (5.120_999_999, 52.090_000_007)),
            ((142_026.181, 453_095.608), (5.197_999_999, 52.066_000_002)),
            ((196_611.369, 501_543.124), (6.000_000_005, 52.500_000_005)),
            ((23_969.303, 391_804.958), (3.499_999_995, 51.500_000_002)),
            ((262_770.290, 580_468.022), (6.999_999_997, 53.200_000_001)),
        ];
        for ((x, y), (elon, elat)) in cases {
            let (lon, lat) = to_wgs84(x, y);
            assert!(close((lon, lat), (elon, elat), 0.01), "{lon},{lat}");
        }
    }

    #[test]
    fn round_trip() {
        for (lon, lat) in [(4.892, 52.373), (5.121, 52.09), (7.0, 53.2)] {
            let (x, y) = to_rd(lon, lat);
            let (lon2, lat2) = to_wgs84(x, y);
            assert!(close((lon2, lat2), (lon, lat), 0.001), "{lon2},{lat2}");
        }
    }
}
