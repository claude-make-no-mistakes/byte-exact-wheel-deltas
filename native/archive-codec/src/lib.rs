use miniz_oxide::deflate::compress_to_vec;
use std::slice;

/// Return zero when the output does not fit the supplied buffer.
///
/// # Safety
/// The pointers must address valid disjoint buffers of their declared lengths.
#[no_mangle]
pub unsafe extern "C" fn wheel_miniz_deflate(
    input: *const u8,
    length: usize,
    level: u8,
    output: *mut u8,
    capacity: usize,
) -> usize {
    if input.is_null() || output.is_null() || level > 9 {
        return 0;
    }
    let compressed = compress_to_vec(slice::from_raw_parts(input, length), level);
    if compressed.len() > capacity {
        return 0;
    }
    slice::from_raw_parts_mut(output, capacity)[..compressed.len()].copy_from_slice(&compressed);
    compressed.len()
}
